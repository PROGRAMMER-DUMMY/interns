"""The rendered blueprint keeps its reviewable structure: a mermaid graph built
from the workspace's real table names, provisioning, per-KPI model mapping,
cost-annotated options, the DQ plan, the additive-only statement, and a
"rule that fired" callout next to every choice.

Plus the two lifecycle guarantees: a confirmed blueprint renders a DIFF on the
next run, and confirmation demands a real human name.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.blueprint.cli import confirm_blueprint
from core.blueprint.renderer import (
    ADDITIVE_LINE,
    diff_payloads,
    prepare_blueprint,
    render_mermaid,
)
from core.onboarding.workspace.delegation import routing_for

GIB = 1024 ** 3

# Real intake shape: `core/intake/interview.py` question ids, answered with the
# option ids that interview persists.
ANSWERS = {
    "consumers": "bi_dashboard,self_serve_analyst",
    "freshness_sla": "daily",
    "calendar_and_restatement": "append_only",
    "target_latency": "batch_daily",
    # The alignment gate: prepare-blueprint refuses until the playback is confirmed.
    "playback_confirm": "confirmed",
    "as_it_was_then": "as_it_is_now",
    "hot_filters": "region and month; exploratory for now",
    "concurrency": "12 at peak",
    "lineage_reproducibility": "not_required",
    "compute_budget_constraints": "serverless_ok",
    "non_sql_logic": "sql_only",
    "failure_tolerance": "full_rerun_ok",
}

DISCOVERY = {
    "connector": "s3",
    "scanned_at": "2026-08-05T00:00:00Z",
    "tables": [
        {
            "name": "alpha_events",
            "path": "s3://bucket/alpha_events/",
            "format": "parquet",
            "size_bytes": 20 * GIB,
            "row_estimate": 1_000_000,
            "arrival_pattern": "periodic",
            "arrival_evidence": "12 file(s), median gap 86400s",
            "is_streaming": False,
        },
        {
            "name": "beta_ref",
            "path": "s3://bucket/beta_ref/",
            "format": "delta",
            "size_bytes": 1 * GIB,
            "row_estimate": 500,
            "arrival_pattern": "continuous",
            "arrival_evidence": "40 file(s), median gap 30s",
            "is_streaming": True,
        },
    ],
    "working_set_estimate_bytes": 21 * GIB,
    "notes": "",
}

KPIS = [{"kpi_id": "KPI-01", "name": "First metric"}, {"kpi_id": "KPI-02", "name": "Second metric"}]

# Past the alignment gate, but with nothing else answered: every decision blocks.
ONLY_PLAYBACK_CONFIRMED = {"playback_confirm": "confirmed"}


def _workspace(tmp: str, *, discovery=DISCOVERY, answers=ANSWERS, kpis=KPIS) -> Path:
    root = Path(tmp)
    ws = root / "workspaces" / "fixture"
    (ws / "interns" / "generated" / "intake").mkdir(parents=True)
    (ws / "interns" / "generated" / "contracts").mkdir(parents=True)
    (ws / "interns" / "generated" / "intake" / "discovery.json").write_text(
        json.dumps(discovery), encoding="utf-8"
    )
    (ws / "interns" / "generated" / "intake" / "intake_answers.json").write_text(
        json.dumps({k: {"answer": v, "answered_by": "fixture", "at": "t"} for k, v in answers.items()}),
        encoding="utf-8",
    )
    (ws / "interns" / "generated" / "contracts" / "kpi_registry.json").write_text(
        json.dumps({"kpis": kpis}), encoding="utf-8"
    )
    (ws / "workspace_settings.json").write_text(
        json.dumps(
            {
                "source_declaration": {
                    "type": "s3",
                    "location": "s3://bucket/",
                    "format_hint": "parquet",
                    "credential_ref": "uc_storage_cred_ref",
                    "declared_by": "fixture",
                    "declared_at": "t",
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def _render(tmp: str, **kwargs):
    root = _workspace(tmp, **kwargs)
    result = prepare_blueprint(root, "workspaces/fixture")
    markdown = (root / result.current_markdown_path).read_text(encoding="utf-8")
    payload = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
    return root, result, markdown, payload


class FreshRenderTests(unittest.TestCase):
    def test_sections_are_present_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            expected = [
                "# Solution blueprint",
                "## Pipeline",
                "## Provisioning (all additive)",
                "## Ingestion per table",
                "## KPI -> model mapping",
                "## Velocity lane",
                "## Engine",
                "## Compute",
                "## Modeling",
                "## Data quality",
                "## Orchestration",
                "## Confirm",
            ]
            positions = [markdown.find(heading) for heading in expected]
            self.assertNotIn(-1, positions, f"missing heading: {expected[positions.index(-1)] if -1 in positions else ''}")
            self.assertEqual(sorted(positions), positions, "sections rendered out of order")

    def test_mermaid_block_is_plain_fenced_and_names_real_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            self.assertIn("```mermaid\nflowchart TD", markdown)
            block = markdown.split("```mermaid", 1)[1].split("```", 1)[0]
            self.assertIn("alpha_events", block)
            self.assertIn("beta_ref", block)
            self.assertIn("bronze.alpha_events", block)
            self.assertIn("silver.beta_ref", block)
            self.assertIn("gold.gold_kpi_01", block)
            self.assertIn("Airflow DAG fixture_pipeline", block)
            self.assertIn("Dashboard", block)

    def test_ingestion_method_is_named_per_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            self.assertIn("COPY INTO (batch, idempotent by file)", markdown)
            self.assertIn("Auto Loader (streaming, checkpointed)", markdown)

    def test_provisioning_lists_the_credential_reference_not_a_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            self.assertIn("| external location | `fixture_root -> s3://bucket/` | uc_storage_cred_ref |", markdown)
            self.assertIn("| catalog | `fixture` |", markdown)
            self.assertIn("| schema | `fixture.gold` |", markdown)
            self.assertIn("Credential *references* only.", markdown)

    def test_per_kpi_model_mapping_is_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            self.assertIn("| `KPI-01` | First metric | `gold_kpi_01` | `fixture.gold.gold_kpi_01` |", markdown)
            self.assertIn("| `KPI-02` | Second metric | `gold_kpi_02` | `fixture.gold.gold_kpi_02` |", markdown)

    def test_engine_and_compute_each_show_the_rule_that_fired_and_two_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            self.assertIn("> **Rule that fired: `ENG-", markdown)
            self.assertIn("> **Rule that fired: `CMP-", markdown)
            self.assertIn("| Option | Cost / trade-off |", markdown)
            # engine, compute, velocity lane
            self.assertEqual(3, markdown.count("| Option | Cost / trade-off |"))
            self.assertIn("(chosen) | selected by `", markdown)

    def test_evidence_carries_the_citation_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            self.assertIn("threshold source: https://", markdown)

    def test_dq_summary_covers_all_three_layers_with_severity(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            section = markdown.split("## Data quality", 1)[1].split("## Orchestration", 1)[0]
            for layer in ("bronze", "silver", "gold"):
                self.assertIn(f"| {layer} |", section)
            self.assertIn("warn_or_quarantine", section)

    def test_additive_statement_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            self.assertIn(ADDITIVE_LINE, markdown)
            self.assertIn("NOTHING HAS BEEN CREATED YET.", markdown)

    def test_no_emoji_and_ascii_markers_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            self.assertTrue(all(ord(ch) < 0x2190 for ch in markdown), "non-ASCII symbol in blueprint")

    def test_decisions_contract_is_written_next_to_the_blueprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _, _ = _render(tmp)
            path = root / "workspaces/fixture/interns/generated/contracts/blueprint_decisions.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("blueprint_decisions.json", payload["artifact_type"])
            self.assertTrue(payload["decisions"])

    def test_empty_discovery_says_so_instead_of_inventing_tables(self):
        empty = {"connector": "s3", "tables": [], "working_set_estimate_bytes": 1 * GIB}
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp, discovery=empty, kpis=[])
            self.assertIn("_No tables discovered.", markdown)
            self.assertIn("_No KPIs in the registry yet;", markdown)

    def test_a_legacy_blueprint_artifact_is_preserved_not_clobbered(self):
        """`prepare-solution-blueprint` writes the same path during the strangler
        overlap; its artifact must survive a run of this one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            out = root / "workspaces/fixture/interns/reports/solution_blueprint"
            out.mkdir(parents=True)
            (out / "current.json").write_text(
                json.dumps({"generated_by": "prepare-solution-blueprint", "status": "approved"}),
                encoding="utf-8",
            )
            (out / "current.md").write_text("# legacy", encoding="utf-8")
            prepare_blueprint(root, "workspaces/fixture")
            legacy = json.loads((out / "current.legacy.json").read_text(encoding="utf-8"))
            self.assertEqual("prepare-solution-blueprint", legacy["generated_by"])
            self.assertEqual("# legacy", (out / "current.legacy.md").read_text(encoding="utf-8"))
            self.assertEqual(
                "prepare-blueprint",
                json.loads((out / "current.json").read_text(encoding="utf-8"))["generated_by"],
            )

    def test_blocked_decision_is_surfaced_at_the_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, result, markdown, _ = _render(tmp, answers=ONLY_PLAYBACK_CONFIRMED)
            self.assertFalse(result.ok)
            self.assertIn("[blocked]", markdown)
            self.assertIn("non_sql_logic", markdown)


class VelocityLaneTests(unittest.TestCase):
    def test_lane_section_shows_the_rule_that_fired(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            section = markdown.split("## Velocity lane", 1)[1].split("## Engine", 1)[0]
            self.assertIn("> **Rule that fired: `VEL-", section)
            self.assertIn("batch", section)

    def test_measured_arrival_pattern_is_shown_per_source_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, markdown, _ = _render(tmp)
            ingestion = markdown.split("## Ingestion per table", 1)[1].split("## KPI", 1)[0]
            self.assertIn("| Arrival |", ingestion)
            self.assertIn("periodic", ingestion)
            self.assertIn("continuous", ingestion)

    def test_realtime_serving_blocks_with_the_named_missing_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            answers = {**ANSWERS, "target_latency": "realtime_serving"}
            _, result, markdown, _ = _render(tmp, answers=answers)
            self.assertFalse(result.ok)
            self.assertIn("online_store_serving_edge", markdown)


class PlaybackGateTests(unittest.TestCase):
    def test_prepare_blueprint_refuses_until_the_playback_is_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            answers = {k: v for k, v in ANSWERS.items() if k != "playback_confirm"}
            root = _workspace(tmp, answers=answers)
            with self.assertRaises(ValueError) as ctx:
                prepare_blueprint(root, "workspaces/fixture")
            message = str(ctx.exception)
            self.assertIn("intake_playback", message)
            self.assertIn("--question playback_confirm --answer confirmed", message)

    def test_the_cli_turns_the_refusal_into_a_clean_non_zero_exit(self):
        from core.blueprint.cli import prepare_blueprint_main

        with tempfile.TemporaryDirectory() as tmp:
            answers = {k: v for k, v in ANSWERS.items() if k != "playback_confirm"}
            root = _workspace(tmp, answers=answers)
            code = prepare_blueprint_main(
                ["--workspace", "workspaces/fixture", "--repo-root", str(root)]
            )
            self.assertEqual(1, code)


class MermaidUnitTests(unittest.TestCase):
    def test_declared_kpi_sources_draw_direct_edges(self):
        structure = {
            "workspace": "workspaces/fixture",
            "schemas": {"bronze": "bronze", "silver": "silver", "gold": "gold"},
            "source": {"location": "s3://bucket/", "connector": "s3"},
            "tables": [
                {"source_name": "alpha", "format": "parquet", "size_bytes": GIB, "ingestion": "COPY INTO"}
            ],
            "kpi_models": [{"kpi_id": "K1", "dbt_model": "gold_k1", "source_tables": ["alpha"]}],
            "schedule": "0 4 * * *",
            "dag_name": "fixture_pipeline",
        }
        block = render_mermaid(structure, {"decisions": []})
        self.assertIn("s_alpha --> g_k1", block)
        self.assertNotIn("SILVER", block)

    def test_undeclared_kpi_sources_route_through_one_silver_node(self):
        structure = {
            "workspace": "workspaces/fixture",
            "schemas": {"bronze": "bronze", "silver": "silver", "gold": "gold"},
            "source": {"location": "s3://bucket/", "connector": "s3"},
            "tables": [
                {"source_name": "alpha", "format": "parquet", "size_bytes": GIB, "ingestion": "COPY INTO"}
            ],
            "kpi_models": [{"kpi_id": "K1", "dbt_model": "gold_k1", "source_tables": []}],
            "schedule": "0 4 * * *",
            "dag_name": "fixture_pipeline",
        }
        block = render_mermaid(structure, {"decisions": []})
        self.assertIn("per-KPI sources not declared", block)
        self.assertIn("SILVER --> g_k1", block)


class ConfirmationTests(unittest.TestCase):
    def test_agent_identity_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _, _ = _render(tmp)
            with self.assertRaises(PermissionError):
                confirm_blueprint(root, "workspaces/fixture", confirmed_by="claude")

    def test_blocked_blueprint_cannot_be_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _, _ = _render(tmp, answers=ONLY_PLAYBACK_CONFIRMED)
            with self.assertRaises(ValueError) as ctx:
                confirm_blueprint(root, "workspaces/fixture", confirmed_by="Dr. Reviewer")
            self.assertIn("blocked", str(ctx.exception))

    def test_human_confirmation_writes_the_confirmed_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _, _ = _render(tmp)
            out = confirm_blueprint(root, "workspaces/fixture", confirmed_by="Dr. Reviewer")
            self.assertEqual("human", out["source"])
            payload = json.loads(
                (root / out["confirmed_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual("confirmed", payload["status"])
            self.assertEqual("Dr. Reviewer", payload["confirmation"]["confirmed_by"])


class DiffModeTests(unittest.TestCase):
    def test_rerun_after_confirmation_renders_a_diff_not_a_fresh_blueprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _, _ = _render(tmp)
            confirm_blueprint(root, "workspaces/fixture", confirmed_by="Dr. Reviewer")

            # A new table appears and the workspace grows past the 50 GiB tier.
            intake = root / "workspaces/fixture/interns/generated/intake/discovery.json"
            grown = json.loads(intake.read_text(encoding="utf-8"))
            grown["tables"].append(
                {
                    "name": "gamma_new",
                    "path": "s3://bucket/gamma_new/",
                    "format": "parquet",
                    "size_bytes": 60 * GIB,
                    "is_streaming": False,
                }
            )
            grown["working_set_estimate_bytes"] = 81 * GIB
            intake.write_text(json.dumps(grown), encoding="utf-8")

            result = prepare_blueprint(root, "workspaces/fixture")
            markdown = (root / result.current_markdown_path).read_text(encoding="utf-8")
            self.assertEqual("diff", result.mode)
            self.assertIn("# Solution blueprint - changes since confirmation", markdown)
            self.assertIn("Confirmed by `Dr. Reviewer`", markdown)
            self.assertIn("### Added tables", markdown)
            self.assertIn("`gamma_new`", markdown)
            self.assertIn("### Changed decisions", markdown)
            self.assertIn("## Re-confirm", markdown)
            # The fresh-render sections are replaced, not appended.
            self.assertNotIn("## Provisioning (all additive)", markdown)
            self.assertIn(ADDITIVE_LINE, markdown)

    def test_unchanged_rerun_reports_no_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _, _ = _render(tmp)
            confirm_blueprint(root, "workspaces/fixture", confirmed_by="Dr. Reviewer")
            result = prepare_blueprint(root, "workspaces/fixture")
            markdown = (root / result.current_markdown_path).read_text(encoding="utf-8")
            self.assertEqual("diff", result.mode)
            self.assertIn("_No change since the confirmed blueprint._", markdown)

    def test_a_changed_answer_is_named_next_to_the_decision_it_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _, _ = _render(tmp)
            confirm_blueprint(root, "workspaces/fixture", confirmed_by="Dr. Reviewer")

            # The user changes their mind: one consumer class instead of two.
            answers_path = root / "workspaces/fixture/interns/generated/intake/intake_answers.json"
            answers = json.loads(answers_path.read_text(encoding="utf-8"))
            answers["consumers"]["answer"] = "ml_features"
            answers_path.write_text(json.dumps(answers), encoding="utf-8")

            result = prepare_blueprint(root, "workspaces/fixture")
            markdown = (root / result.current_markdown_path).read_text(encoding="utf-8")
            payload = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))

            changed = payload["diff"]["changed_decisions"]
            self.assertTrue(changed)
            modeling = next(c for c in changed if c["decision"] == "modeling")
            self.assertEqual("feature_tables_plus_star", modeling["to_choice"])
            self.assertIn("consumers", modeling["driven_by"])
            self.assertIn("driven by changed answer", markdown)
            self.assertIn("`consumers`", markdown)

    def test_an_unchanged_rerun_has_no_changed_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _, _ = _render(tmp)
            confirm_blueprint(root, "workspaces/fixture", confirmed_by="Dr. Reviewer")
            result = prepare_blueprint(root, "workspaces/fixture")
            payload = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual([], payload["diff"]["changed_decisions"])

    def test_diff_payload_classifies_added_removed_and_changed(self):
        confirmed = {
            "decisions": {
                "decisions": [
                    {"decision": "engine", "rule_id": "ENG-R8", "choice": "sql_dbt_warehouse", "status": "decided"},
                    {"decision": "modeling", "rule_id": "R7", "choice": "scd2_on_history_dimensions", "status": "decided"},
                ]
            },
            "structure": {"tables": [{"source_name": "alpha"}], "kpi_models": [{"kpi_id": "K1"}]},
        }
        current = {
            "decisions": {
                "decisions": [
                    {"decision": "engine", "rule_id": "ENG-R8", "choice": "pyspark", "status": "decided"},
                    {"decision": "compute", "rule_id": "CMP-R6", "choice": "serverless_sql_small", "status": "decided"},
                ]
            },
            "structure": {
                "tables": [{"source_name": "alpha"}, {"source_name": "beta"}],
                "kpi_models": [],
            },
        }
        diff = diff_payloads(confirmed, current)
        self.assertEqual(["compute/CMP-R6"], [d["key"] for d in diff["decisions_added"]])
        self.assertEqual(["modeling/R7"], [d["key"] for d in diff["decisions_removed"]])
        self.assertEqual(["engine/ENG-R8"], [d["key"] for d in diff["decisions_changed"]])
        self.assertEqual("pyspark", diff["decisions_changed"][0]["to"]["choice"])
        self.assertEqual(["beta"], diff["tables_added"])
        self.assertEqual(["K1"], diff["kpis_removed"])


class StageRoutingTests(unittest.TestCase):
    def test_blueprint_payload_and_result_carry_the_blueprint_review_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, result, _, payload = _render(tmp)
            roster = routing_for("blueprint_review")
            self.assertTrue(roster["agents"], "blueprint_review routes no agent")
            for label, emitted in (("payload", payload), ("result", result.summary())):
                with self.subTest(payload=label):
                    self.assertEqual(emitted["stage"], "blueprint_review")
                    self.assertEqual(emitted["required_specialists"], roster["agents"])
                    self.assertEqual(emitted["suggested_skills"], roster["skills"])

    def test_velocity_lane_decision_carries_its_own_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, payload = _render(tmp)
            lanes = [
                decision
                for decision in payload["decisions"]["decisions"]
                if decision["decision"] == "velocity_lane"
            ]
            self.assertTrue(lanes, "fixture produced no velocity_lane decision")
            roster = routing_for("velocity_lane_choice")
            self.assertTrue(roster["agents"], "velocity_lane_choice routes no agent")
            for lane in lanes:
                self.assertEqual(lane["stage"], "velocity_lane_choice")
                self.assertEqual(lane["required_specialists"], roster["agents"])
                self.assertEqual(lane["suggested_skills"], roster["skills"])


if __name__ == "__main__":
    unittest.main()

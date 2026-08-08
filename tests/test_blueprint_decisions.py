"""Each decision-table rule id reaches its expected choice from fixture inputs,
and a missing fact BLOCKS the decision instead of being guessed.

The blocking half is the point of the slice: the predecessor
(`core/onboarding/kpi/engine_recommender.py`) fabricated a size as
`rows * cols * 16` when bytes were unknown, which silently decided the tier.
"""
from __future__ import annotations

import unittest
from core.blueprint import decisions

from core.blueprint.decisions import (
    INTAKE_FACT_BRIDGE,
    STATUS_BLOCKED,
    STATUS_DECIDED,
    build_facts,
    build_payload,
    eval_condition,
    evaluate_table,
    load_tables,
)

GIB = 1024 ** 3
TIB = 1024 ** 4

# REAL intake shape: keys are `core/intake/interview.py` question ids and values
# are the option ids that interview's `_normalize_answer` persists (multi_select
# as a comma-joined string). Individual tests override one question so exactly
# one rule's condition changes.
BASE_ANSWERS = {
    "consumers": "bi_dashboard",
    "freshness_sla": "daily",
    "calendar_and_restatement": "append_only",
    "as_it_was_then": "as_it_is_now",
    "hot_filters": "region and month; the set is stable",
    "concurrency": "about 8 dashboard users at peak",
    "lineage_reproducibility": "not_required",
    "compute_budget_constraints": "serverless_ok",
    "non_sql_logic": "sql_only",
    "failure_tolerance": "full_rerun_ok",
}


def _answers(**overrides):
    merged = {**BASE_ANSWERS, **overrides}
    return {
        k: {"answer": v, "answered_by": "fixture", "source": "human", "at": "2026-08-05"}
        for k, v in merged.items()
    }


def _discovery(working_set=1 * GIB, streaming=False, sizes=(1 * GIB,), arrival=None):
    pattern = arrival or ("continuous" if streaming else "periodic")
    return {
        "connector": "s3",
        "scanned_at": "2026-08-05T00:00:00Z",
        "tables": [
            {
                "name": f"t{i}",
                "path": f"s3://bucket/t{i}/",
                "format": "parquet",
                "size_bytes": size,
                "row_estimate": 100,
                "arrival_pattern": pattern,
                "arrival_evidence": "fixture",
                "is_streaming": pattern == "continuous",
            }
            for i, size in enumerate(sizes)
        ],
        "working_set_estimate_bytes": working_set,
        "notes": "",
    }


def _facts(discovery=None, kpis=1, cloud=True, join_depth=1, connector=None, **answer_overrides):
    return build_facts(
        discovery=discovery if discovery is not None else _discovery(),
        intake_answers=_answers(**answer_overrides),
        kpi_summary={
            "kpis": [{"kpi_id": f"KPI-{i:02d}", "name": "x"} for i in range(kpis)],
            "max_join_depth": join_depth,
        },
        source_declaration={
            "type": connector or ("s3" if cloud else "local_files"),
            "location": "s3://bucket/",
            "credential_ref": "cred_ref",
        },
    )


def _fired(decisions, name):
    return [d for d in decisions if d.decision == name]


def _first(decisions, name):
    return next(d for d in decisions if d.decision == name)


class EngineRuleTests(unittest.TestCase):
    def _engine(self, **kwargs):
        return _first(evaluate_table("engine", _facts(**kwargs)), "engine")

    def test_eng_r1_non_sql_logic_forces_pyspark(self):
        decision = self._engine(non_sql_logic="non_sql_needed")
        self.assertEqual(("ENG-R1", "pyspark"), (decision.rule_id, decision.choice))

    def test_eng_r2_above_one_tib_forces_pyspark(self):
        decision = self._engine(discovery=_discovery(working_set=2 * TIB, sizes=(2 * TIB,)))
        self.assertEqual(("ENG-R2", "pyspark"), (decision.rule_id, decision.choice))

    def test_eng_r3_fifty_gib_selects_warehouse(self):
        decision = self._engine(discovery=_discovery(working_set=60 * GIB, sizes=(60 * GIB,)))
        self.assertEqual(("ENG-R3", "sql_dbt_warehouse"), (decision.rule_id, decision.choice))

    def test_eng_r4_streaming_source_stays_on_warehouse(self):
        decision = self._engine(discovery=_discovery(working_set=2 * GIB, streaming=True))
        self.assertEqual(("ENG-R4", "sql_dbt_warehouse"), (decision.rule_id, decision.choice))

    def test_eng_r5_heavy_joins_select_warehouse(self):
        decision = self._engine(join_depth=3)
        self.assertEqual(("ENG-R5", "sql_dbt_warehouse"), (decision.rule_id, decision.choice))

    def test_eng_r6_multi_consumer_selects_warehouse(self):
        decision = self._engine(consumers="bi_dashboard,self_serve_analyst")
        self.assertEqual(("ENG-R6", "sql_dbt_warehouse"), (decision.rule_id, decision.choice))

    def test_eng_r7_local_small_workspace_selects_polars(self):
        decision = self._engine(cloud=False)
        self.assertEqual(("ENG-R7", "polars_single_node"), (decision.rule_id, decision.choice))

    def test_eng_r8_cloud_small_workspace_defaults_to_warehouse_with_polars_alternative(self):
        decision = self._engine()
        self.assertEqual(("ENG-R8", "sql_dbt_warehouse"), (decision.rule_id, decision.choice))
        self.assertIn("polars_single_node", [a["choice"] for a in decision.alternatives])

    def test_polars_choice_carries_the_read_only_writer_constraint(self):
        decision = self._engine(cloud=False)
        self.assertTrue(any(note.startswith("ENG-C1") for note in decision.notes))


class ComputeRuleTests(unittest.TestCase):
    def _compute(self, **kwargs):
        from core.blueprint.decisions import evaluate_all

        return _first(evaluate_all(_facts(**kwargs)), "compute")

    def test_cmp_r0_single_node_engine_gets_the_host(self):
        decision = self._compute(cloud=False)
        self.assertEqual(("CMP-R0", "single_node_host"), (decision.rule_id, decision.choice))

    def test_cmp_r1_no_serverless_forces_classic(self):
        decision = self._compute(compute_budget_constraints="network_isolated")
        self.assertEqual(("CMP-R1", "classic_job_cluster"), (decision.rule_id, decision.choice))

    def test_cmp_r2_long_pyspark_job_goes_classic(self):
        decision = self._compute(non_sql_logic="non_sql_needed", failure_tolerance="must_resume", compute_budget_constraints="cost_capped")
        self.assertEqual(("CMP-R2", "classic_job_cluster"), (decision.rule_id, decision.choice))

    def test_cmp_r3_short_pyspark_job_goes_serverless(self):
        decision = self._compute(non_sql_logic="non_sql_needed")
        self.assertEqual(("CMP-R3", "serverless_jobs"), (decision.rule_id, decision.choice))

    def test_cmp_r4_steady_long_warehouse_load_goes_classic_pro(self):
        decision = self._compute(compute_budget_constraints="cost_capped", failure_tolerance="must_resume")
        self.assertEqual(("CMP-R4", "classic_pro_sql_warehouse"), (decision.rule_id, decision.choice))

    def test_cmp_r5_under_five_gib_gets_the_smallest_warehouse(self):
        decision = self._compute(discovery=_discovery(working_set=2 * GIB))
        self.assertEqual(("CMP-R5", "serverless_sql_2x_small"), (decision.rule_id, decision.choice))

    def test_cmp_r6_under_fifty_gib_gets_small(self):
        decision = self._compute(discovery=_discovery(working_set=20 * GIB, sizes=(20 * GIB,)))
        self.assertEqual(("CMP-R6", "serverless_sql_small"), (decision.rule_id, decision.choice))

    def test_cmp_r7_under_one_tib_gets_medium(self):
        decision = self._compute(discovery=_discovery(working_set=500 * GIB, sizes=(500 * GIB,)))
        self.assertEqual(("CMP-R7", "serverless_sql_medium"), (decision.rule_id, decision.choice))

    def test_every_compute_choice_carries_a_priced_alternative(self):
        decision = self._compute()
        self.assertTrue(decision.alternatives)
        self.assertTrue(all(a["cost_note"] for a in decision.alternatives))


class ModelingRuleTests(unittest.TestCase):
    def _modeling(self, **kwargs):
        return _fired(evaluate_table("modeling", _facts(**kwargs)), "modeling")

    def test_r1_regulatory_across_three_systems_selects_data_vault(self):
        decisions = self._modeling(
            consumers="regulatory", lineage_reproducibility="required"
        )
        self.assertEqual(("R1", "data_vault_silver_star_gold"), (decisions[0].rule_id, decisions[0].choice))

    def test_r2_ml_consumer_adds_feature_tables(self):
        decisions = self._modeling(consumers="ml_features")
        self.assertEqual(("R2", "feature_tables_plus_star"), (decisions[0].rule_id, decisions[0].choice))

    def test_r3_operational_app_with_sub_minute_sla_gets_a_synced_table(self):
        decisions = self._modeling(consumers="operational_app", freshness_sla="sub_minute")
        self.assertEqual(("R3", "synced_key_grain_table"), (decisions[0].rule_id, decisions[0].choice))

    def test_r4_single_consumer_stable_shape_few_kpis_selects_obt(self):
        decisions = self._modeling(kpis=2)
        self.assertEqual(("R4", "obt_from_conformed_silver"), (decisions[0].rule_id, decisions[0].choice))

    def test_r5_multi_consumer_selects_the_conformed_star_default(self):
        decisions = self._modeling(consumers="bi_dashboard,exec_reporting")
        self.assertEqual(("R5", "conformed_star_gold"), (decisions[0].rule_id, decisions[0].choice))

    def test_r6_event_shaped_source_considers_activity_schema(self):
        decisions = self._modeling(kpis=9, discovery=_discovery(streaming=True))
        self.assertEqual(("R6", "activity_schema_or_event_fact"), (decisions[0].rule_id, decisions[0].choice))

    def test_r9_terminal_default_is_the_conformed_star(self):
        decisions = self._modeling(kpis=9)
        self.assertEqual(("R9", "conformed_star_gold"), (decisions[0].rule_id, decisions[0].choice))

    def test_r7_scd2_is_a_modifier_on_top_of_the_base_rule(self):
        decisions = self._modeling(as_it_was_then="as_it_was_then")
        rules = [d.rule_id for d in decisions]
        self.assertIn("R7", rules)
        self.assertNotEqual("R7", rules[0])
        self.assertEqual(
            "scd2_on_history_dimensions",
            next(d.choice for d in decisions if d.rule_id == "R7"),
        )

    def test_r8_hot_stable_shape_under_r5_adds_an_obt_projection(self):
        decisions = self._modeling(consumers="bi_dashboard,exec_reporting")
        self.assertEqual(
            "obt_projection_off_star",
            next(d.choice for d in decisions if d.rule_id == "R8"),
        )


class VelocityRuleTests(unittest.TestCase):
    """The velocity lane is a routed decision like engine/compute, not a setting."""

    def _lane(self, **kwargs):
        return _first(evaluate_table("velocity", _facts(**kwargs)), "velocity_lane")

    def test_vel_r1_realtime_serving_blocks_on_the_missing_serving_edge(self):
        """Honest block: the platform has no online-store serving edge (audit M4)."""
        decision = self._lane(target_latency="realtime_serving")
        self.assertEqual(STATUS_BLOCKED, decision.status)
        self.assertEqual("VEL-R1", decision.rule_id)
        self.assertIsNone(decision.choice)
        self.assertEqual(["online_store_serving_edge"], decision.missing_facts)

    def test_vel_r2_streaming_needs_a_checkpointable_connector(self):
        decision = self._lane(
            target_latency="streaming_continuous",
            discovery=_discovery(arrival="continuous"),
            freshness_sla="minutes",
        )
        self.assertEqual(("VEL-R2", "streaming"), (decision.rule_id, decision.choice))

    def test_vel_r3_streaming_on_an_uncheckpointable_source_degrades_to_micro_batch(self):
        facts = _facts(target_latency="streaming_continuous", connector="uc_existing")
        decision = _first(evaluate_table("velocity", facts), "velocity_lane")
        self.assertEqual(("VEL-R3", "micro_batch"), (decision.rule_id, decision.choice))

    def test_vel_r4_and_r5_follow_the_answered_lane(self):
        self.assertEqual("micro_batch", self._lane(target_latency="micro_batch_hourly").choice)
        self.assertEqual("batch", self._lane(target_latency="batch_daily").choice)

    def test_the_lane_is_derived_from_measurement_when_the_question_was_skipped(self):
        facts = _facts(discovery=_discovery(arrival="periodic"), freshness_sla="hourly")
        self.assertEqual("micro_batch", facts["target_latency"].value)
        self.assertEqual("micro_batch", _first(evaluate_table("velocity", facts), "velocity_lane").choice)

    def test_an_answer_beats_the_derived_lane(self):
        facts = _facts(
            discovery=_discovery(arrival="periodic"),
            freshness_sla="daily",
            target_latency="micro_batch_hourly",
        )
        self.assertEqual("micro_batch", facts["target_latency"].value)
        self.assertEqual("asked", facts["target_latency"].source)

    def test_neither_measured_nor_answered_blocks_naming_the_intake_question(self):
        facts = _facts(discovery=_discovery(arrival="unknown"))
        self.assertNotIn("target_latency", facts)
        decision = _first(evaluate_table("velocity", facts), "velocity_lane")
        self.assertEqual(STATUS_BLOCKED, decision.status)
        self.assertIn("target_latency", decision.missing_facts)

    def test_the_lane_is_a_fact_the_modeling_table_can_read(self):
        from core.blueprint.decisions import evaluate_all

        decisions = evaluate_all(
            _facts(target_latency="micro_batch_hourly", discovery=_discovery(arrival="periodic"))
        )
        lane = _first(decisions, "velocity_lane")
        self.assertEqual("micro_batch", lane.choice)
        modeling = _fired(decisions, "modeling")
        self.assertIn("inferred_member_dimensions", [d.choice for d in modeling])

    def test_r10_also_fires_on_a_restatement_calendar(self):
        from core.blueprint.decisions import evaluate_all

        decisions = evaluate_all(
            _facts(
                target_latency="batch_daily",
                calendar_and_restatement="restate_within_window",
            )
        )
        r10 = next(d for d in decisions if d.rule_id == "R10")
        self.assertEqual("inferred_member_dimensions", r10.choice)

    def test_a_plain_batch_workspace_does_not_get_inferred_members(self):
        from core.blueprint.decisions import evaluate_all

        decisions = evaluate_all(_facts(target_latency="batch_daily"))
        self.assertNotIn("inferred_member_dimensions", [d.choice for d in decisions])
        self.assertFalse([d for d in decisions if d.status == STATUS_BLOCKED])


class DataQualityRuleTests(unittest.TestCase):
    def test_all_three_layers_are_always_placed(self):
        decisions = _fired(evaluate_table("dq", _facts()), "dq")
        self.assertEqual(["DQ-R1", "DQ-R2", "DQ-R3"], [d.rule_id for d in decisions])

    def test_dq_r4_fires_for_a_regulatory_consumer(self):
        decisions = _fired(evaluate_table("dq", _facts(consumers="regulatory")), "dq")
        self.assertIn("DQ-R4", [d.rule_id for d in decisions])

    def test_bronze_is_never_fail_hard(self):
        bronze = next(d for d in _fired(evaluate_table("dq", _facts()), "dq") if d.rule_id == "DQ-R1")
        self.assertIn("severity: warn_or_quarantine", bronze.notes)


class BlockedOnMissingFactTests(unittest.TestCase):
    def test_missing_working_set_blocks_the_engine_decision(self):
        """No byte count anywhere -> blocked, naming the fact. Never a guessed size."""
        facts = build_facts(
            discovery={"connector": "s3", "tables": [{"name": "t0", "format": "parquet"}]},
            intake_answers=_answers(),
            kpi_summary={"kpis": []},
            source_declaration={"type": "s3"},
        )
        self.assertNotIn("working_set_bytes", facts)
        decision = _first(evaluate_table("engine", facts), "engine")
        self.assertEqual(STATUS_BLOCKED, decision.status)
        self.assertIsNone(decision.choice)
        self.assertEqual(["working_set_bytes"], decision.missing_facts)
        self.assertEqual("ENG-R2", decision.rule_id)

    def test_partially_sized_tables_do_not_produce_a_largest_table_fact(self):
        discovery = _discovery()
        discovery["tables"].append({"name": "unsized", "format": "csv"})
        facts = build_facts(discovery=discovery, intake_answers=_answers())
        self.assertNotIn("largest_table_bytes", facts)

    def test_unanswered_intake_blocks_at_the_first_rule_that_needs_it(self):
        facts = build_facts(discovery=_discovery(), intake_answers={}, source_declaration={"type": "s3"})
        decision = _first(evaluate_table("engine", facts), "engine")
        self.assertEqual(STATUS_BLOCKED, decision.status)
        self.assertEqual(["non_sql_logic"], decision.missing_facts)

    def test_blocked_payload_lists_the_missing_facts_and_stays_not_ok(self):
        payload = build_payload(
            workspace="workspaces/fixture",
            discovery=_discovery(),
            intake_answers={},
            kpi_summary={"kpis": []},
            source_declaration={"type": "s3"},
        )
        self.assertTrue(payload["blocked_facts"])
        blocked = [d for d in payload["decisions"] if d["status"] == STATUS_BLOCKED]
        self.assertTrue(blocked)
        for decision in blocked:
            self.assertIsNone(decision["choice"])
            self.assertTrue(decision["missing_facts"])

    def test_a_false_branch_beats_an_unknown_branch(self):
        """AND with one false condition does not match -- it must not block."""
        facts = _facts()
        del facts["working_set_bytes"]
        result, missing = eval_condition(
            {"non_sql_logic": {"eq": True}, "working_set_bytes": {"gte": 1}}, facts
        )
        self.assertFalse(result)
        self.assertEqual([], missing)


class IntakeBridgeTests(unittest.TestCase):
    """The bridge is the only place the interview's vocabulary meets the tables'."""

    def test_every_bridged_question_id_exists_in_the_interview(self):
        from core.intake.interview import QUESTIONS_BY_ID

        for question_id in INTAKE_FACT_BRIDGE:
            self.assertIn(question_id, QUESTIONS_BY_ID)

    def test_every_translated_option_id_is_offered_by_its_question(self):
        """A translator that keys on an option id the interview never offers is a
        silent dead branch. Checked against the interview's own option lists."""
        from core.intake.interview import QUESTIONS_BY_ID

        expected = {
            "consumers": _CONSUMERS_IN_TESTS,
            "freshness_sla": {"sub_minute", "minutes", "hourly", "daily", "weekly_or_monthly"},
            "as_it_was_then": {"as_it_is_now", "as_it_was_then"},
            "lineage_reproducibility": {"required", "not_required"},
            "compute_budget_constraints": {"serverless_ok", "cost_capped", "network_isolated"},
            "non_sql_logic": {"sql_only", "non_sql_needed"},
            "failure_tolerance": {"full_rerun_ok", "must_resume"},
        }
        for question_id, option_ids in expected.items():
            offered = {o["option_id"] for o in QUESTIONS_BY_ID[question_id]["options"]}
            self.assertEqual(offered, option_ids, f"option ids drifted for {question_id}")

    def test_real_answers_translate_to_facts(self):
        facts = _facts(
            consumers="regulatory,ml_features",
            freshness_sla="hourly",
            as_it_was_then="as_it_was_then",
            lineage_reproducibility="required",
            compute_budget_constraints="network_isolated",
            non_sql_logic="non_sql_needed",
            failure_tolerance="must_resume",
            concurrency="peaks around 25 dashboard users",
        )
        self.assertEqual(["regulatory", "ml_features"], facts["consumer_classes"].value)
        self.assertEqual(2, facts["consumer_class_count"].value)
        self.assertEqual("hourly", facts["latency_sla"].value)
        self.assertIs(True, facts["scd2_required"].value)
        self.assertIs(True, facts["lineage_required"].value)
        self.assertIs(False, facts["serverless_allowed"].value)
        self.assertEqual("network_isolated", facts["budget_posture"].value)
        self.assertIs(True, facts["non_sql_logic"].value)
        self.assertEqual("must_resume", facts["resumability"].value)
        self.assertEqual(25, facts["peak_concurrent_queries"].value)
        self.assertEqual("stable", facts["query_shape"].value)
        for fact in facts.values():
            if fact.source == "asked":
                self.assertIn("intake_answers.", fact.origin)

    def test_real_answers_drive_a_full_decision_set(self):
        decisions = build_payload(
            workspace="workspaces/fixture",
            discovery=_discovery(working_set=90 * GIB, sizes=(90 * GIB,)),
            intake_answers=_answers(
                consumers="regulatory,exec_reporting",
                lineage_reproducibility="required",
                compute_budget_constraints="network_isolated",
            ),
            kpi_summary={"kpis": [{"kpi_id": "KPI-01", "name": "x"}], "max_join_depth": 1},
            source_declaration={"type": "s3"},
        )["decisions"]
        fired = {(d["decision"], d["rule_id"]): d["choice"] for d in decisions}
        self.assertEqual("sql_dbt_warehouse", fired[("engine", "ENG-R3")])
        self.assertEqual("classic_job_cluster", fired[("compute", "CMP-R1")])
        self.assertEqual("data_vault_silver_star_gold", fired[("modeling", "R1")])
        self.assertEqual("bronze_immutable_retention_floor", fired[("dq", "DQ-R4")])
        self.assertFalse([d for d in decisions if d["status"] == STATUS_BLOCKED])

    def test_an_unrecognised_option_id_leaves_the_fact_absent(self):
        """Contract drift must block loudly, never coerce to a default branch."""
        facts = _facts(freshness_sla="whenever", non_sql_logic="maybe")
        self.assertNotIn("latency_sla", facts)
        self.assertNotIn("non_sql_logic", facts)

    def test_unanswered_questions_leave_their_facts_absent(self):
        facts = build_facts(discovery=_discovery(), intake_answers={})
        for name in ("consumer_classes", "latency_sla", "scd2_required", "serverless_allowed"):
            self.assertNotIn(name, facts)

    def test_free_text_without_the_word_leaves_query_shape_absent(self):
        self.assertNotIn("query_shape", _facts(hot_filters="mostly by region"))
        self.assertNotIn("query_shape", _facts(hot_filters="stable, but also exploratory"))
        self.assertEqual("exploratory", _facts(hot_filters="exploratory slicing")["query_shape"].value)

    def test_volume_prose_is_never_turned_into_bytes(self):
        facts = build_facts(
            discovery={"connector": "s3", "tables": [{"name": "t0", "format": "csv"}]},
            intake_answers=_answers(),
        )
        self.assertNotIn("working_set_bytes", facts)
        self.assertNotIn("largest_table_bytes", facts)

    def test_join_complexity_is_measured_from_the_feature_mapping(self):
        from core.blueprint.decisions import _max_join_depth

        mapping = {
            "kpis": [
                {"features": [{"source_columns": [{"dataset": "a/x.csv", "column": "c"}]}]},
                {
                    "features": [
                        {"source_columns": [{"dataset": "a/x.csv", "column": "c"}]},
                        {"source_columns": [{"dataset": "b/y.csv", "column": "d"}]},
                        {"source_columns": [{"dataset": "c/z.csv", "column": "e"}]},
                    ]
                },
            ]
        }
        self.assertEqual(2, _max_join_depth(mapping))
        self.assertIsNone(_max_join_depth({}))

    def test_no_rule_depends_on_a_fact_nothing_can_produce(self):
        """Every fact a table reads must be derivable from discovery, the source
        declaration, the KPI summary, or the intake bridge -- otherwise its rule
        blocks forever with no way for anyone to unblock it."""
        from core.blueprint.decisions import _condition_facts

        producible = set(_facts().keys()) | {
            "engine",  # injected from the engine decision before the compute table
            "velocity_lane",  # injected from the velocity decision before modeling
            "budget_posture",
            "resumability",
            "scd2_required",
            "lineage_required",
            "event_shaped",
            "largest_table_bytes",
            "restatement_mode",
            "target_latency",
        }
        for name, table in load_tables().items():
            for rule in list(table.get("rules") or []) + list(table.get("modifiers") or []):
                for fact in _condition_facts(rule.get("when")):
                    with self.subTest(table=name, rule=rule["id"], fact=fact):
                        self.assertIn(fact, producible | DELIBERATELY_UNPRODUCIBLE)

    def test_the_unproducible_facts_are_exactly_the_documented_honest_blocks(self):
        """A fact nothing can produce is only allowed where the platform does not
        yet have the capability, so the decision BLOCKS instead of promising it."""
        from core.blueprint.decisions import _condition_facts

        producible = set(_facts().keys()) | {
            "engine",
            "velocity_lane",
            "budget_posture",
            "resumability",
            "scd2_required",
            "lineage_required",
            "event_shaped",
            "largest_table_bytes",
            "restatement_mode",
            "target_latency",
        }
        referenced = {
            fact
            for table in load_tables().values()
            for rule in list(table.get("rules") or []) + list(table.get("modifiers") or [])
            for fact in _condition_facts(rule.get("when"))
        }
        self.assertEqual(DELIBERATELY_UNPRODUCIBLE, referenced - producible)


# Facts NO input can produce today. Each one is a capability the platform has not
# built, named so the decision that needs it blocks honestly instead of guessing.
# `online_store_serving_edge`: audit M4 (operational / reverse-ETL sync missing).
DELIBERATELY_UNPRODUCIBLE = {"online_store_serving_edge"}

_CONSUMERS_IN_TESTS = {
    "exec_reporting",
    "self_serve_analyst",
    "bi_dashboard",
    "ml_features",
    "operational_app",
    "regulatory",
    "external_share",
}


class PayloadShapeTests(unittest.TestCase):
    def test_every_decision_carries_rule_evidence_and_a_citation(self):
        payload = build_payload(
            workspace="workspaces/fixture",
            discovery=_discovery(),
            intake_answers=_answers(),
            kpi_summary={"kpis": [{"kpi_id": "KPI-01", "name": "x"}]},
            source_declaration={"type": "s3"},
        )
        self.assertTrue(payload["decisions"])
        for decision in payload["decisions"]:
            self.assertIn(
                decision["decision"], {"engine", "compute", "velocity_lane", "modeling", "dq"}
            )
            self.assertTrue(decision["rule_id"])
            self.assertTrue(decision["evidence"])
            self.assertIn(decision["source"], {"measured", "asked", "default"})
            if decision["status"] == STATUS_DECIDED:
                self.assertTrue(decision["citation"].startswith("https://"))

    def test_facts_record_their_provenance(self):
        payload = build_payload(
            workspace="workspaces/fixture",
            discovery=_discovery(),
            intake_answers=_answers(),
            source_declaration={"type": "s3"},
        )
        self.assertEqual("measured", payload["facts"]["working_set_bytes"]["source"])
        self.assertEqual("asked", payload["facts"]["latency_sla"]["source"])
        self.assertTrue(payload["facts"]["working_set_bytes"]["origin"])


class TableIntegrityTests(unittest.TestCase):
    def test_every_rule_has_an_id_rationale_citation_and_priced_alternatives(self):
        for name, table in load_tables().items():
            rules = list(table.get("rules") or []) + list(table.get("modifiers") or [])
            self.assertTrue(rules, f"{name} has no rules")
            for rule in rules:
                with self.subTest(table=name, rule=rule.get("id")):
                    self.assertTrue(rule.get("id"))
                    self.assertTrue(rule.get("choice"))
                    self.assertTrue(rule.get("rationale"))
                    self.assertTrue(str(rule.get("citation", "")).startswith("https://"))
                    for alt in rule.get("alternatives") or []:
                        self.assertTrue(alt.get("cost_note"))

    def test_rule_ids_are_unique_per_table(self):
        for name, table in load_tables().items():
            ids = [r["id"] for r in list(table.get("rules") or []) + list(table.get("modifiers") or [])]
            self.assertEqual(len(ids), len(set(ids)), f"duplicate rule id in {name}")

    def test_no_workspace_or_domain_vocabulary_leaked_into_the_tables(self):
        import pathlib

        banned = ("rcm", "healthcare", "hospital", "claim", "patient", "payer")
        for path in (pathlib.Path(__file__).resolve().parents[1] / "core" / "blueprint" / "tables").glob("*.yaml"):
            text = path.read_text(encoding="utf-8").lower()
            for word in banned:
                self.assertNotIn(word, text, f"{path.name} mentions {word}")


if __name__ == "__main__":
    unittest.main()


class DecisiveUnknownTests(unittest.TestCase):
    """An unknown blocks only when it could change the answer.

    Found live: `join_complexity` was unmeasurable until data landed in Unity
    Catalog, but landing data needed a confirmed blueprint, which needed this
    decision -- a real deadlock. Two later rules already agreed on the same
    engine, so the missing fact could not have changed the outcome.

    The relaxation is narrow ON PURPOSE: any disagreement among the later rules,
    or a second unknown, still blocks.
    """

    TABLE = {
        "decision": "engine",
        "mode": "first_match",
        "rules": [
            {"id": "R1", "when": {"mystery": {"eq": "heavy"}}, "choice": "spark"},
            {"id": "R2", "when": {"consumers": {"gte": 2}}, "choice": "warehouse"},
            {"id": "R3", "when": {"size": {"lt": 100}}, "choice": "warehouse"},
        ],
    }

    def _facts(self, **kw):
        return {
            k: decisions.Fact(name=k, value=v, source="measured", origin="test")
            for k, v in kw.items()
        }

    def test_unknown_that_cannot_change_the_answer_decides(self):
        out = decisions.evaluate_table(
            "engine", self._facts(consumers=4, size=10), table=self.TABLE
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].status, decisions.STATUS_DECIDED)
        self.assertEqual(out[0].choice, "warehouse")
        self.assertTrue(
            any("cannot change it" in e for e in out[0].evidence),
            "the decision must say WHY it did not block",
        )

    def test_unknown_still_blocks_when_later_rules_disagree(self):
        table = dict(self.TABLE)
        table["rules"] = [
            {"id": "R1", "when": {"mystery": {"eq": "heavy"}}, "choice": "spark"},
            {"id": "R2", "when": {"consumers": {"gte": 2}}, "choice": "warehouse"},
            {"id": "R3", "when": {"size": {"lt": 100}}, "choice": "polars"},
        ]
        out = decisions.evaluate_table(
            "engine", self._facts(consumers=4, size=10), table=table
        )
        self.assertEqual(out[0].status, decisions.STATUS_BLOCKED)
        self.assertIn("mystery", out[0].missing_facts)

    def test_unknown_still_blocks_when_no_later_rule_fires(self):
        out = decisions.evaluate_table(
            "engine", self._facts(consumers=1, size=999), table=self.TABLE
        )
        self.assertEqual(out[0].status, decisions.STATUS_BLOCKED)

    def test_a_second_unknown_still_blocks(self):
        out = decisions.evaluate_table("engine", self._facts(size=10), table=self.TABLE)
        self.assertEqual(out[0].status, decisions.STATUS_BLOCKED)

    def test_a_known_earlier_rule_still_wins(self):
        """The relaxation must not reorder anything that was already decidable."""
        out = decisions.evaluate_table(
            "engine", self._facts(mystery="heavy", consumers=4, size=10), table=self.TABLE
        )
        self.assertEqual(out[0].choice, "spark")
        self.assertEqual(out[0].rule_id, "R1")

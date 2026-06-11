"""Tests for evidence-driven KPI metric/cuts derivation.

Evidence is SYNTHETIC and non-healthcare (orders / shipments) to prove the
derivation is generic and never leans on a baked domain vocabulary.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from core.onboarding.kpi.generation_workflow import _build_draft_kpis
from core.onboarding.kpi.metric_derivation import (
    HIGH_CONFIDENCE_THRESHOLD,
    columns_from_profile_index,
    derive_metric_and_cuts,
)

MODULE_PATH = Path(__file__).resolve().parents[1] / "core/onboarding/kpi/metric_derivation.py"


def _orders_columns() -> list[dict]:
    return [
        {
            "column": "order_id",
            "dataset": "datasets/orders.csv",
            "dtype": "Int64",
            "row_count": 5000,
            "n_unique": 5000,
            "sample_values": ["1001", "1002", "1003"],
        },
        {
            "column": "order_date",
            "dataset": "datasets/orders.csv",
            "dtype": "Date",
            "row_count": 5000,
            "n_unique": 365,
            "sample_values": ["2021-01-03", "2021-02-11", "2021-03-08"],
        },
        {
            "column": "order_value",
            "dataset": "datasets/orders.csv",
            "dtype": "Float64",
            "row_count": 5000,
            "n_unique": 4800,
            "sample_values": ["12.50", "99.00", "7.25"],
        },
        {
            "column": "region",
            "dataset": "datasets/orders.csv",
            "dtype": "String",
            "row_count": 5000,
            "n_unique": 4,
            "sample_values": ["North", "South", "East", "West"],
        },
    ]


class CountMetricTimeCutTest(unittest.TestCase):
    def test_how_many_orders_each_month(self) -> None:
        result = derive_metric_and_cuts("How many orders each month?", _orders_columns())
        self.assertEqual(result["intent"], "count")
        # count distinct of the matched high-cardinality id column.
        self.assertEqual(result["metric"]["value"], "count(distinct order_id)")
        self.assertGreaterEqual(result["metric"]["confidence"], HIGH_CONFIDENCE_THRESHOLD)
        # month time-cut derived from the Date column via profile dtype.
        self.assertEqual(result["cuts"]["value"], "month(order_date)")
        self.assertTrue(result["high_confidence"])
        self.assertTrue(result["metric"]["evidence"])


class AverageMetricCategoricalCutTest(unittest.TestCase):
    def test_average_order_value_by_region(self) -> None:
        result = derive_metric_and_cuts("Average order value by region", _orders_columns())
        self.assertEqual(result["intent"], "average")
        self.assertEqual(result["metric"]["value"], "avg(order_value)")
        self.assertGreaterEqual(result["metric"]["confidence"], HIGH_CONFIDENCE_THRESHOLD)
        self.assertEqual(result["cuts"]["value"], "region")
        self.assertTrue(result["high_confidence"])


class SumMetricTest(unittest.TestCase):
    def test_total_order_value_per_year(self) -> None:
        result = derive_metric_and_cuts("Total order value per year", _orders_columns())
        self.assertEqual(result["intent"], "sum")
        self.assertEqual(result["metric"]["value"], "sum(order_value)")
        self.assertEqual(result["cuts"]["value"], "year(order_date)")


class AmbiguousQuestionTest(unittest.TestCase):
    def test_ambiguous_question_low_confidence_empty(self) -> None:
        # No measure noun maps to any profiled column; intent unclear.
        result = derive_metric_and_cuts(
            "What about the situation with things?", _orders_columns()
        )
        self.assertEqual(result["intent"], "unknown")
        self.assertEqual(result["metric"]["value"], "")
        self.assertEqual(result["cuts"]["value"], "")
        self.assertFalse(result["high_confidence"])

    def test_average_with_no_matching_measure_leaves_metric_empty(self) -> None:
        # "average tonnage" -> no numeric column named/glossed tonnage.
        result = derive_metric_and_cuts("Average tonnage by region", _orders_columns())
        self.assertEqual(result["metric"]["value"], "")
        self.assertFalse(result["high_confidence"])
        # Proposal/alternatives still attached for the panel.
        self.assertTrue(
            result["metric"]["alternatives"] or result["metric"]["confidence"] < HIGH_CONFIDENCE_THRESHOLD
        )


class ShareIntentTest(unittest.TestCase):
    def test_percentage_share_stays_low_confidence(self) -> None:
        result = derive_metric_and_cuts(
            "What percentage of orders came from each region?", _orders_columns()
        )
        self.assertEqual(result["intent"], "share")
        # Share needs an explicit numerator/denominator -> never auto-populated.
        self.assertEqual(result["metric"]["value"], "")
        self.assertFalse(result["high_confidence"])
        self.assertTrue(result["metric"]["alternatives"])


class TopNIntentTest(unittest.TestCase):
    def test_top_n_ranking_defers_to_panel(self) -> None:
        # A top-N ranking needs a coordinated grain + ranking-measure decision the
        # evidence pass can't make safely (the "by <measure>" clause names the
        # ranking measure, not the grain; the executable form is the bare measure
        # plus a name-derived ORDER BY/LIMIT). Like share, it is deliberately left
        # below threshold so the definition panel asks -- never auto-emitted as an
        # executable-but-wrong "top N by ..." metric.
        result = derive_metric_and_cuts(
            "Top 5 regions by total order value", _orders_columns()
        )
        self.assertEqual(result["intent"], "top_n")
        self.assertEqual(result["metric"]["value"], "")
        self.assertEqual(result["cuts"]["value"], "")
        self.assertFalse(result["high_confidence"])
        # The ranking proposal still rides along for the panel.
        self.assertTrue(
            any("top 5" in str(alt.get("value", "")) for alt in result["metric"]["alternatives"])
        )


class MultiTableTimeGrainTest(unittest.TestCase):
    """An event count must be dated by ITS OWN table's date, not a stray date
    column from another table that happens to sort first in the profiles."""

    def _two_table_cols(self) -> list[dict]:
        # customers.csv (signup_date) profiled BEFORE orders.csv (order_date) so
        # the first global date column is the wrong one for an orders question.
        return [
            {"column": "id", "dataset": "datasets/customers.csv", "dtype": "Int64", "row_count": 800, "n_unique": 800},
            {"column": "signup_date", "dataset": "datasets/customers.csv", "dtype": "Date", "row_count": 800, "n_unique": 790, "sample_values": ["2019-04-02"]},
            {"column": "id", "dataset": "datasets/orders.csv", "dtype": "Int64", "row_count": 5000, "n_unique": 5000},
            {"column": "order_date", "dataset": "datasets/orders.csv", "dtype": "Date", "row_count": 5000, "n_unique": 1200, "sample_values": ["2021-03-04"]},
            {"column": "customer_id", "dataset": "datasets/orders.csv", "dtype": "Int64", "row_count": 5000, "n_unique": 800},
        ]

    def test_event_count_uses_its_own_table_date(self) -> None:
        result = derive_metric_and_cuts("How many orders occurred each year?", self._two_table_cols())
        self.assertEqual(result["intent"], "count")
        # Table-name match anchors the count to orders.id and the time grain to
        # the orders-table date, NOT customers.signup_date (the first date col).
        self.assertEqual(result["cuts"]["value"], "year(order_date)")
        self.assertNotIn("signup_date", result["cuts"]["value"])

    def _entity_attr_vs_event_cols(self) -> list[dict]:
        # Entity table (customers: own id + a static attribute date) plus an
        # event table referencing it via an entity-NAMED column (not id-shaped,
        # so it cannot win the count itself) with its own event date.
        return [
            {"column": "id", "dataset": "datasets/customers.csv", "dtype": "String", "row_count": 800, "n_unique": 800},
            {"column": "signup_date", "dataset": "datasets/customers.csv", "dtype": "Date", "row_count": 800, "n_unique": 790, "sample_values": ["2019-04-02"]},
            {"column": "id", "dataset": "datasets/orders.csv", "dtype": "String", "row_count": 5000, "n_unique": 5000},
            {"column": "customer", "dataset": "datasets/orders.csv", "dtype": "String", "row_count": 5000, "n_unique": 800},
            {"column": "order_date", "dataset": "datasets/orders.csv", "dtype": "Date", "row_count": 5000, "n_unique": 1200, "sample_values": ["2021-03-04"]},
        ]

    def test_passive_event_on_entity_makes_temporal_anchor_ambiguous(self) -> None:
        # "customers were onboarded each quarter": the counted entity is
        # customers, but the PASSIVE phrasing names an event done TO the
        # entity, and orders (a table referencing customers via an
        # entity-named column) carries its own date. Anchoring to the entity's
        # static attribute date (signup_date) would silently bucket an event
        # series by an entity attribute — so the anchor must be left ambiguous
        # (below threshold, candidates listed) for the definition gate.
        result = derive_metric_and_cuts(
            "How many unique customers were onboarded each quarter over time?",
            self._entity_attr_vs_event_cols(),
        )
        self.assertEqual(result["intent"], "count")
        self.assertEqual(result["cuts"]["value"], "")
        self.assertLess(result["cuts"]["confidence"], 0.6)
        alt_values = [str(a.get("value", "")) for a in result["cuts"]["alternatives"]]
        self.assertTrue(any("signup_date" in v for v in alt_values))
        self.assertTrue(any("order_date" in v for v in alt_values))

    def test_active_event_count_keeps_own_table_anchor(self) -> None:
        # Active phrasing ("orders occurred") counts the dated events
        # themselves; the same-table anchor stays confident even though
        # customers also references nothing — autonomy preserved.
        result = derive_metric_and_cuts(
            "How many orders occurred each month over time?", self._two_table_cols()
        )
        self.assertEqual(result["cuts"]["value"], "month(order_date)")


class EntityCountAmbiguityTest(unittest.TestCase):
    """A count question whose nouns name TWO countable entities (the entity
    and its event) must not silently pick one — counting events as entities
    answers a different question."""

    def _entity_event_cols(self) -> list[dict]:
        return [
            {"column": "Id", "dataset": "datasets/customers.csv", "dtype": "String", "row_count": 800, "n_unique": 800},
            {"column": "signup_date", "dataset": "datasets/customers.csv", "dtype": "Date", "row_count": 800, "n_unique": 790, "sample_values": ["2019-04-02"]},
            {"column": "Id", "dataset": "datasets/orders.csv", "dtype": "String", "row_count": 5000, "n_unique": 5000},
            {"column": "order_date", "dataset": "datasets/orders.csv", "dtype": "Date", "row_count": 5000, "n_unique": 1200, "sample_values": ["2021-03-04"]},
        ]

    def test_two_term_backed_entities_block_the_count(self) -> None:
        result = derive_metric_and_cuts(
            "How many customers reordered within 30 days of a previous order?",
            self._entity_event_cols(),
        )
        self.assertEqual(result["intent"], "count")
        self.assertEqual(result["metric"]["value"], "")
        self.assertLess(result["metric"]["confidence"], 0.6)
        # Both candidate counts ride along for the definition gate.
        alt_values = " ".join(str(a.get("value", "")) for a in result["metric"]["alternatives"])
        self.assertIn("count(distinct", alt_values)
        reasons = " ".join(str(a.get("reason", "")) for a in result["metric"]["alternatives"])
        self.assertIn("customers", reasons)
        self.assertIn("orders", reasons)

    def test_single_entity_count_stays_confident(self) -> None:
        result = derive_metric_and_cuts(
            "How many total orders occurred each year?", self._entity_event_cols()
        )
        self.assertEqual(result["metric"]["value"], "count(distinct Id)")
        self.assertGreaterEqual(result["metric"]["confidence"], 0.6)


class DictionaryEvidenceTest(unittest.TestCase):
    def test_dictionary_gloss_resolves_measure(self) -> None:
        # Column physically named "gmv" only matches the question through its
        # data-dictionary description -- proving dictionary-backed matching.
        cols = [
            {
                "column": "shipment_id",
                "dataset": "datasets/shipments.csv",
                "dtype": "Int64",
                "row_count": 1000,
                "n_unique": 1000,
            },
            {
                "column": "gmv",
                "dataset": "datasets/shipments.csv",
                "dtype": "Float64",
                "row_count": 1000,
                "n_unique": 990,
                "dictionary_description": "gross merchandise revenue per shipment",
            },
        ]
        result = derive_metric_and_cuts("Total revenue", cols)
        self.assertEqual(result["intent"], "sum")
        self.assertEqual(result["metric"]["value"], "sum(gmv)")


class ProfileIndexFlattenTest(unittest.TestCase):
    def test_columns_from_profile_index(self) -> None:
        payload = {
            "profiles": [
                {
                    "path": "datasets/shipments.csv",
                    "row_count": 200,
                    "schema": {"ship_date": "Date", "weight": "Float64"},
                    "columns": [
                        {"name": "ship_date", "sample_values": ["2022-01-01"], "n_unique": 50},
                        {"name": "weight", "sample_values": ["10.5"], "n_unique": 180},
                    ],
                }
            ]
        }
        cols = columns_from_profile_index(payload)
        names = {c["column"] for c in cols}
        self.assertEqual(names, {"ship_date", "weight"})
        weight = next(c for c in cols if c["column"] == "weight")
        self.assertEqual(weight["dtype"], "Float64")
        self.assertEqual(weight["n_unique"], 180)


class BuildDraftKpisWiringTest(unittest.TestCase):
    """Prove the derivation populates empty metric/cuts inside _build_draft_kpis."""

    def _write_orders_profile(self, root: Path) -> None:
        prof = root / "workspaces" / "OrdersWS" / "interns/generated/profiles"
        prof.mkdir(parents=True)
        (prof / "profile_index.json").write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "path": "datasets/orders.csv",
                            "row_count": 5000,
                            "schema": {
                                "order_id": "Int64",
                                "order_date": "Date",
                                "order_value": "Float64",
                                "region": "String",
                            },
                            "columns": [
                                {"name": "order_id", "n_unique": 5000, "sample_values": ["1", "2"]},
                                {"name": "order_date", "n_unique": 365, "sample_values": ["2021-01-01"]},
                                {"name": "order_value", "n_unique": 4800, "sample_values": ["12.5"]},
                                {"name": "region", "n_unique": 4, "sample_values": ["North"]},
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_empty_metric_cuts_get_derived_high_conf_and_attached_low_conf(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write_orders_profile(root)
            session = {
                "workspace": "workspaces/OrdersWS",
                "existing_kpis": [
                    {"name": "How many orders each month?", "description": "", "cuts": "", "metric": "", "source": "q.sql"},
                    {"name": "Average order value by region", "description": "", "cuts": "", "metric": "", "source": "q.sql"},
                    {"name": "What about things?", "description": "", "cuts": "", "metric": "", "source": "q.sql"},
                ],
                "quality_score": {"kpis": []},
            }
            drafts = _build_draft_kpis(session, root)
            by_q = {d["business_question"]: d for d in drafts}

            count_kpi = by_q["How many orders each month?"]
            self.assertEqual(count_kpi["metric"], "count(distinct order_id)")
            self.assertEqual(count_kpi["cuts"], "month(order_date)")

            avg_kpi = by_q["Average order value by region"]
            self.assertEqual(avg_kpi["metric"], "avg(order_value)")
            self.assertEqual(avg_kpi["cuts"], "region")

            # Ambiguous KPI: cells stay empty, but the proposal rides along so a
            # panel can ask. The definition-blocker gate handles it downstream.
            ambiguous = by_q["What about things?"]
            self.assertEqual(ambiguous["metric"], "")
            self.assertEqual(ambiguous["cuts"], "")
            self.assertIn("metric_derivation", ambiguous)

    def test_no_profiles_present_leaves_cells_empty_no_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session = {
                "workspace": "workspaces/NoProfilesWS",
                "existing_kpis": [
                    {"name": "How many orders each month?", "description": "", "cuts": "", "metric": "", "source": "q.sql"},
                ],
                "quality_score": {"kpis": []},
            }
            drafts = _build_draft_kpis(session, root)
            # count(*) is still a safe count fallback even without profiles, but
            # there is no date column so the time-cut stays empty.
            self.assertEqual(drafts[0]["cuts"], "")
            self.assertIn("metric_derivation", drafts[0])


class StringTypedDateEvidenceTest(unittest.TestCase):
    """A date column profiled as a plain String (the common CSV case) whose name
    carries no date token must still be recognized as the time grain on its
    sampled VALUES alone. This is the exact shape that left grains unresolved
    before: ``start`` / ``stop`` columns holding ISO timestamps."""

    def _event_columns(self) -> list[dict]:
        # ``shipments`` table with string-typed timestamp columns named start/stop
        # (no "date"/"time" token in the name) — generic, non-domain.
        return [
            {
                "column": "id",
                "dataset": "datasets/shipments.csv",
                "dtype": "String",
                "row_count": 9000,
                "n_unique": None,  # sample profile: no distinct count available
                "sample_values": [
                    "57674e7c-cf62-8e01", "8ed5344d-3c97-e02d", "a7741cb1-2e54-d5ce",
                ],
            },
            {
                "column": "start",
                "dataset": "datasets/shipments.csv",
                "dtype": "String",
                "row_count": 9000,
                "n_unique": None,
                "sample_values": [
                    "2018-08-31T05:51:44Z", "2015-06-07T11:15:35Z", "2015-09-25T21:00:58Z",
                ],
            },
            {
                "column": "carrier",
                "dataset": "datasets/shipments.csv",
                "dtype": "String",
                "row_count": 9000,
                "n_unique": None,
                "sample_values": ["UPS", "FedEx", "DHL"],
            },
        ]

    def test_string_iso_timestamp_recognized_as_time_grain(self) -> None:
        result = derive_metric_and_cuts(
            "How many shipments occurred each year?", self._event_columns()
        )
        self.assertEqual(result["cuts"]["value"], "year(start)")
        self.assertGreaterEqual(result["cuts"]["confidence"], HIGH_CONFIDENCE_THRESHOLD)

    def test_count_grain_recovered_from_table_name_without_cardinality(self) -> None:
        # No n_unique to confirm the id by cardinality, but the table is named
        # after the counted entity -> its id is the count grain.
        result = derive_metric_and_cuts(
            "How many shipments occurred each year?", self._event_columns()
        )
        self.assertEqual(result["metric"]["value"], "count(distinct id)")
        self.assertGreaterEqual(result["metric"]["confidence"], HIGH_CONFIDENCE_THRESHOLD)

    def test_non_date_strings_are_not_temporal(self) -> None:
        # The carrier column (plain labels) is a categorical cut, never a date —
        # it must not be wrapped in a time-grain function like year()/month().
        result = derive_metric_and_cuts(
            "How many shipments by carrier?", self._event_columns()
        )
        self.assertEqual(result["cuts"]["value"], "carrier")
        self.assertNotIn("year(", result["cuts"]["value"])
        self.assertNotIn("month(", result["cuts"]["value"])


class MeasureSpecificityTests(unittest.TestCase):
    """The measure must be chosen by the SPECIFIC term, not the entity/table word
    (which is true of every column there), and by dictionary gloss when present."""

    def _two_cost_columns(self) -> list[dict]:
        # Both columns are numeric measures in the `tickets` table; the question's
        # entity word ("ticket") matches neither uniquely, so the SPECIFIC term
        # ("net") must decide — not "base" or the entity.
        return [
            {"column": "net_value", "dataset": "datasets/tickets.csv", "dtype": "Float64",
             "row_count": 100, "n_unique": 90, "sample_values": ["10.0"]},
            {"column": "base_ticket_value", "dataset": "datasets/tickets.csv", "dtype": "Float64",
             "row_count": 100, "n_unique": 80, "sample_values": ["5.0"]},
        ]

    def test_specific_term_outranks_entity_named_sibling(self) -> None:
        result = derive_metric_and_cuts(
            "What is the average net ticket value by region?", self._two_cost_columns()
        )
        self.assertEqual(result["metric"]["value"], "avg(net_value)")

    def test_entity_named_measure_still_selected_when_no_specific_term(self) -> None:
        # "ticket value" with only the entity word available (no rival specific
        # term) must still pick the entity-named measure — specificity is an
        # additive tie-break, never a demotion.
        cols = [
            {"column": "ticket_value", "dataset": "datasets/tickets.csv", "dtype": "Float64",
             "row_count": 100, "n_unique": 90, "sample_values": ["10.0"]},
            {"column": "ticket_id", "dataset": "datasets/tickets.csv", "dtype": "Int64",
             "row_count": 100, "n_unique": 100, "sample_values": ["1"]},
        ]
        result = derive_metric_and_cuts("Average ticket value by region?", cols)
        self.assertEqual(result["metric"]["value"], "avg(ticket_value)")

    def test_dictionary_gloss_drives_measure_when_name_is_opaque(self) -> None:
        # Column names give no signal; the data-dictionary gloss does.
        cols = [
            {"column": "amt_a", "dataset": "datasets/tickets.csv", "dtype": "Float64",
             "row_count": 100, "sample_values": ["10.0"],
             "dictionary_description": "total settled value including all line items"},
            {"column": "amt_b", "dataset": "datasets/tickets.csv", "dtype": "Float64",
             "row_count": 100, "sample_values": ["5.0"],
             "dictionary_description": "base value excluding line items"},
        ]
        result = derive_metric_and_cuts("average total settled value by region", cols)
        self.assertEqual(result["metric"]["value"], "avg(amt_a)")


class GenericityGuardTest(unittest.TestCase):
    def test_no_domain_terms_hardcoded(self) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8").lower()
        # Strip the module docstring example to avoid false positives; we only
        # care that no real domain vocabulary is baked into logic.
        forbidden = [
            "healthcare", "hospital", "rcm", "encounter", "patient",
            "diagnosis", "claim", "payer", "drg", "icd",
        ]
        for term in forbidden:
            self.assertNotIn(
                term, text, f"forbidden domain term '{term}' found in metric_derivation.py"
            )


if __name__ == "__main__":
    unittest.main()

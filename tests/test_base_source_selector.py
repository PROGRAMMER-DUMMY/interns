"""Relationship-graph base-source selection.

The selector replaces the "largest referenced table is the fact" heuristic.
The decisive fixture: three plausible fact tables with similar volumes where
the LARGEST is the WRONG base for the KPI — graph evidence (coverage, grain,
fan-out safety) must pick correctly or surface a near-tie blocker instead of
silently picking by size.
"""
from __future__ import annotations

import unittest

from core.onboarding.relationships.base_source_selector import (
    NEAR_TIE_MARGIN,
    base_source_blocker,
    select_base_source,
)

ORDERS = "workspaces/demo/datasets/sales/orders.csv"
SHIPMENTS = "workspaces/demo/datasets/logistics/shipments.csv"
INVOICES = "workspaces/demo/datasets/billing/invoices.csv"
CARRIERS = "workspaces/demo/datasets/logistics/carrier_ref.csv"
DEPOTS = "workspaces/demo/datasets/logistics/depot_ref.csv"


def _profile(row_count: int, columns: list[str]) -> dict:
    return {"row_count": row_count, "schema": {column: "VARCHAR" for column in columns}}


def _edge(left: str, left_col: str, right: str, right_col: str) -> dict:
    return {
        "left_dataset": left,
        "left_column": left_col,
        "right_dataset": right,
        "right_column": right_col,
        "state": "proven_data_model",
        "executable_usage_policy": {"allowed_in_sql_generation": True},
    }


class DecoyFactFixture(unittest.TestCase):
    """Three fact-like tables; the largest (orders) is the WRONG base for a
    shipment-grain KPI that needs carrier and depot dimensions."""

    def setUp(self) -> None:
        self.profiles = {
            ORDERS: _profile(120_000, ["Id", "Date", "Amount", "Status"]),
            SHIPMENTS: _profile(
                95_000,
                ["Id", "Date", "Amount", "Status", "carrier_key", "depot_key", "TransitDays"],
            ),
            INVOICES: _profile(110_000, ["Id", "Date", "Amount", "Status"]),
            CARRIERS: _profile(40, ["carrier_key", "Name"]),
            DEPOTS: _profile(15, ["depot_key", "Name"]),
        }
        # Edges oriented many->one (fact side on the left).
        self.relationships = [
            _edge(SHIPMENTS, "carrier_key", CARRIERS, "carrier_key"),
            _edge(SHIPMENTS, "depot_key", DEPOTS, "depot_key"),
        ]
        # KPI: transit days by carrier and depot — features resolve across
        # shipments + the two dims; orders/invoices are NOT referenced.
        self.refs = [
            {"dataset": SHIPMENTS, "column": "TransitDays"},
            {"dataset": SHIPMENTS, "column": "Date"},
            {"dataset": CARRIERS, "column": "Name"},
            {"dataset": DEPOTS, "column": "Name"},
        ]

    def test_graph_picks_connected_fact_not_largest(self) -> None:
        selection = select_base_source(
            self.refs,
            self.profiles,
            self.relationships,
            grain_dimensions=["Month(Date)", "Carrier", "Depot"],
        )
        self.assertEqual(selection.base_source, SHIPMENTS)
        self.assertFalse(selection.near_tie)

    def test_largest_table_loses_when_unconnected(self) -> None:
        # Even with orders referenced (a stray feature ref), shipments wins:
        # orders cannot reach the dims via executable edges.
        refs = self.refs + [{"dataset": ORDERS, "column": "Amount"}]
        selection = select_base_source(
            refs,
            self.profiles,
            self.relationships,
            grain_dimensions=["Month(Date)"],
        )
        self.assertEqual(selection.base_source, SHIPMENTS)
        orders = next(c for c in selection.candidates if c.dataset == ORDERS)
        self.assertIn(CARRIERS, orders.unreachable_datasets)

    def test_fan_out_join_is_penalized(self) -> None:
        # A dimension as base must traverse one->many to reach the fact.
        refs = [
            {"dataset": SHIPMENTS, "column": "TransitDays"},
            {"dataset": CARRIERS, "column": "Name"},
        ]
        selection = select_base_source(refs, self.profiles, self.relationships)
        carriers = next(c for c in selection.candidates if c.dataset == CARRIERS)
        self.assertEqual(selection.base_source, SHIPMENTS)
        self.assertTrue(carriers.fan_out_joins)

    def test_pinned_decision_is_honored_verbatim(self) -> None:
        selection = select_base_source(
            self.refs,
            self.profiles,
            self.relationships,
            pinned=INVOICES.replace("/", "\\"),  # path-form differences tolerated
        )
        # INVOICES is not referenced -> pin cannot apply; falls back to scoring.
        self.assertEqual(selection.base_source, SHIPMENTS)

        refs = self.refs + [{"dataset": INVOICES, "column": "Amount"}]
        pinned = select_base_source(
            refs, self.profiles, self.relationships, pinned=INVOICES
        )
        self.assertEqual(pinned.base_source, INVOICES)
        self.assertEqual(pinned.decision_source, "pinned_pipeline_decision")
        self.assertTrue(pinned.pinned_by_decision)
        self.assertFalse(pinned.near_tie)


class NearTieTest(unittest.TestCase):
    def test_structurally_identical_candidates_block(self) -> None:
        profiles = {
            ORDERS: _profile(100_000, ["Id", "Date", "Amount"]),
            INVOICES: _profile(100_000, ["Id", "Date", "Amount"]),
        }
        refs = [
            {"dataset": ORDERS, "column": "Amount"},
            {"dataset": INVOICES, "column": "Amount"},
        ]
        selection = select_base_source(refs, profiles, [], grain_dimensions=["Date"])
        self.assertTrue(selection.near_tie)
        self.assertEqual(sorted(selection.tie_candidates), sorted([ORDERS, INVOICES]))

        blocker = base_source_blocker("kpi_009", selection)
        self.assertEqual(blocker["type"], "base_source_ambiguous")
        self.assertEqual(blocker["kpi_id"], "kpi_009")
        self.assertEqual(len(blocker["candidates"]), 2)
        for candidate in blocker["candidates"]:
            self.assertIn("join_paths", candidate)
            self.assertIn("row_count", candidate)

    def test_clear_winner_is_not_a_near_tie(self) -> None:
        profiles = {
            ORDERS: _profile(100_000, ["Id", "Date", "Amount", "Region"]),
            CARRIERS: _profile(40, ["carrier_key", "Name"]),
        }
        relationships = [_edge(ORDERS, "carrier_key", CARRIERS, "carrier_key")]
        refs = [
            {"dataset": ORDERS, "column": "Amount"},
            {"dataset": ORDERS, "column": "Region"},
            {"dataset": CARRIERS, "column": "Name"},
        ]
        selection = select_base_source(
            refs, profiles, relationships, grain_dimensions=["Region"]
        )
        self.assertEqual(selection.base_source, ORDERS)
        self.assertFalse(selection.near_tie)
        top, runner_up = selection.candidates[0], selection.candidates[1]
        self.assertGreaterEqual(top.score - runner_up.score, NEAR_TIE_MARGIN)


class DegenerateInputsTest(unittest.TestCase):
    def test_no_refs(self) -> None:
        selection = select_base_source([], {}, [])
        self.assertEqual(selection.base_source, "")
        self.assertEqual(selection.decision_source, "no_refs")

    def test_single_candidate(self) -> None:
        profiles = {ORDERS: _profile(10, ["Id"])}
        selection = select_base_source([{"dataset": ORDERS, "column": "Id"}], profiles, [])
        self.assertEqual(selection.base_source, ORDERS)
        self.assertEqual(selection.decision_source, "single_candidate")
        self.assertFalse(selection.near_tie)

    def test_rationale_records_scoring_model(self) -> None:
        profiles = {ORDERS: _profile(10, ["Id"])}
        selection = select_base_source([{"dataset": ORDERS, "column": "Id"}], profiles, [])
        rationale = selection.rationale()
        self.assertEqual(rationale["method"], "relationship_graph_score")
        self.assertEqual(
            rationale["scoring_model"]["row_count_role"], "final_tiebreak_only"
        )
        self.assertEqual(len(rationale["candidates"]), 1)


if __name__ == "__main__":
    unittest.main()

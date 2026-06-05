"""Reusable derived-feature patterns: duration bucket + recurrence self-join.
Generic, non-domain fixtures (shipments / orders)."""
from __future__ import annotations

import unittest

from core.onboarding.features.derivation_patterns import (
    detect_derivation_patterns,
    detect_duration_bucket,
    detect_recurrence_within_window,
)


def _shipments_cols() -> list[dict]:
    return [
        {"column": "id", "dataset": "datasets/shipments.csv", "dtype": "String",
         "sample_values": ["a1", "b2"]},
        {"column": "start", "dataset": "datasets/shipments.csv", "dtype": "String",
         "sample_values": ["2018-08-31T05:51:44Z", "2015-06-07T11:15:35Z"]},
        {"column": "stop", "dataset": "datasets/shipments.csv", "dtype": "String",
         "sample_values": ["2018-09-02T05:51:44Z", "2015-06-09T11:15:35Z"]},
        {"column": "carrier", "dataset": "datasets/shipments.csv", "dtype": "String",
         "sample_values": ["UPS", "FedEx"]},
    ]


def _orders_cols() -> list[dict]:
    return [
        {"column": "customer_id", "dataset": "datasets/orders.csv", "dtype": "String",
         "sample_values": ["c1", "c2"]},
        {"column": "order_date", "dataset": "datasets/orders.csv", "dtype": "String",
         "sample_values": ["2021-01-05", "2021-02-11"]},
        {"column": "amount", "dataset": "datasets/orders.csv", "dtype": "Float64",
         "sample_values": ["10.0", "20.0"]},
    ]


class DurationBucketTests(unittest.TestCase):
    def test_over_24_hours_emits_duration_formula(self) -> None:
        opt = detect_duration_bucket(
            "What percentage of shipments were over 24 hours versus under?",
            _shipments_cols(),
        )
        self.assertIsNotNone(opt)
        self.assertEqual(opt["source_pattern_id"], "duration_bucket")
        self.assertIn("date_diff('hour'", opt["formula"])
        self.assertIn(">= 24", opt["formula"])
        cols = {c["column"] for c in opt["input_columns"]}
        self.assertEqual(cols, {"start", "stop"})
        self.assertTrue(opt["needs_user_confirmation"])

    def test_no_duration_phrase_no_option(self) -> None:
        self.assertIsNone(
            detect_duration_bucket("How many shipments each year?", _shipments_cols())
        )

    def test_no_temporal_pair_no_option(self) -> None:
        cols = [{"column": "id", "dataset": "d.csv", "dtype": "String", "sample_values": ["x"]}]
        self.assertIsNone(detect_duration_bucket("events over 24 hours", cols))


class RecurrenceWindowTests(unittest.TestCase):
    def test_within_30_days_of_previous_emits_self_join(self) -> None:
        opt = detect_recurrence_within_window(
            "How many customers ordered again within 30 days of a previous order?",
            _orders_cols(),
        )
        self.assertIsNotNone(opt)
        self.assertEqual(opt["source_pattern_id"], "recurrence_within_window")
        self.assertIn("EXISTS", opt["formula"])
        self.assertIn("INTERVAL 30 DAY", opt["formula"])
        roles = {c["input_name"] for c in opt["input_columns"]}
        self.assertEqual(roles, {"entity", "event_time"})

    def test_window_without_recurrence_hint_no_option(self) -> None:
        self.assertIsNone(
            detect_recurrence_within_window(
                "How many orders within 30 days of launch?", _orders_cols()
            )
        )


class DispatchTests(unittest.TestCase):
    def test_detect_returns_matching_patterns_only(self) -> None:
        opts = detect_derivation_patterns(
            "shipments over 24 hours", _shipments_cols()
        )
        ids = {o["source_pattern_id"] for o in opts}
        self.assertIn("duration_bucket", ids)
        self.assertNotIn("recurrence_within_window", ids)

    def test_unrelated_question_yields_nothing(self) -> None:
        self.assertEqual(
            detect_derivation_patterns("average amount by carrier", _orders_cols()), []
        )


if __name__ == "__main__":
    unittest.main()

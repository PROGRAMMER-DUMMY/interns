"""Reusable derived-feature patterns: duration bucket + recurrence self-join.
Generic, non-domain fixtures (shipments / orders)."""
from __future__ import annotations

import unittest
from pathlib import Path

from core.onboarding.features import derivation_patterns as _dp
from core.onboarding.features.derivation_patterns import (
    detect_derivation_patterns,
    detect_duration_bucket,
    detect_recurrence_within_window,
)

_MODULE_PATH = Path(_dp.__file__)


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

    def test_exceeds_verb_emits_duration(self) -> None:
        # "exceeds / exceeding" is a common duration-threshold phrasing that the
        # comparator-verb list must cover.
        for phrase in (
            "shipments whose duration exceeds 24 hours",
            "shipments exceeding 24 hours in transit",
        ):
            opt = detect_duration_bucket(phrase, _shipments_cols())
            self.assertIsNotNone(opt, phrase)
            self.assertEqual(opt["source_pattern_id"], "duration_bucket")
            self.assertIn(">= 24", opt["formula"])

    def test_explicit_subtraction_form_emits_duration(self) -> None:
        # "STOP - START > 24 hours" / "stop minus start over 1 day": the explicit
        # interval-subtraction phrasing must resolve to the same date_diff cut.
        for phrase in ("stop - start > 24 hours", "stop minus start over 1 day"):
            opt = detect_duration_bucket(phrase, _shipments_cols())
            self.assertIsNotNone(opt, phrase)
            self.assertEqual(opt["source_pattern_id"], "duration_bucket")
            cols = {c["column"] for c in opt["input_columns"]}
            self.assertEqual(cols, {"start", "stop"})
            # Formula always computes date_diff(start, stop) regardless of the
            # operand order written in the question.
            self.assertIn('date_diff(', opt["formula"])
            self.assertIn('CAST("start" AS TIMESTAMP)', opt["formula"])


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

    def test_noun_form_recurrence_emits_self_join(self) -> None:
        # Noun/standalone recurrence phrasings (recurrence / recurring / reorder /
        # repeat) must hit, not only "...within N days of a previous order".
        for phrase in (
            "customers with a recurrence within 30 days",
            "recurring orders within 30 days",
            "customers who reorder within 30 days",
            "repeat orders within 30 days",
        ):
            opt = detect_recurrence_within_window(phrase, _orders_cols())
            self.assertIsNotNone(opt, phrase)
            self.assertEqual(opt["source_pattern_id"], "recurrence_within_window")
            self.assertIn("INTERVAL 30 DAY", opt["formula"])

    def test_re_prefix_noun_does_not_false_positive(self) -> None:
        # A windowed question whose only "re" word is unrelated (release / review /
        # region) must NOT be treated as recurrence.
        for phrase in (
            "How many orders within 30 days of release?",
            "reviews within 30 days of launch",
            "region totals within 30 days",
        ):
            self.assertIsNone(
                detect_recurrence_within_window(phrase, _orders_cols()), phrase
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


class GenericityGuardTest(unittest.TestCase):
    def test_no_domain_terms_hardcoded(self) -> None:
        # The detection logic must be domain-agnostic: no baked workspace
        # vocabulary. Detection rides on generic temporal/entity-key evidence and
        # generic English duration/recurrence phrasing only.
        text = _MODULE_PATH.read_text(encoding="utf-8").lower()
        forbidden = [
            "healthcare", "hospital", "rcm", "encounter", "patient",
            "diagnosis", "claim", "payer", "drg", "icd",
        ]
        for term in forbidden:
            self.assertNotIn(
                term, text, f"forbidden domain term '{term}' in derivation_patterns.py"
            )


if __name__ == "__main__":
    unittest.main()

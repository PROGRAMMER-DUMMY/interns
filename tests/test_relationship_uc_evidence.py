"""Referential-integrity evidence for Unity-Catalog-backed profiles.

`_left_key_resolution_ratio` proves a join by measuring `|left ∩ right| / |left|`
over observed key values -- the one signal that separates a real foreign key
from two columns that merely share a name. It read the values with
`csv.DictReader` and returned None for anything else, so on a cloud workspace
(Delta tables in UC, no local file) every join fell back to None, could never
reach `proven_data_model`, and needed a human to confirm it by hand. The
value-based check silently switched off on the path that is becoming default.

Two properties are pinned here, and the second is the reason this is not just
"read the values from the warehouse instead":

  1. the ratio is computed IN SQL and comes back as ONE ROW. Materializing
     distinct key sets client-side is fine at GB and fatal at TB -- a
     500M-cardinality key will not fit in memory.
  2. a BOUNDED sample is not an acceptable substitute. Intersecting a sample of
     left keys with a sample of right keys under-counts overlap catastrophically
     and would report a valid FK as broken, so the query must not LIMIT.

Nothing here touches the network: the client seam is a fake that records SQL.
"""
from __future__ import annotations

import unittest

from core.onboarding.relationships.contracts import (
    _left_key_resolution_ratio,
    _uc_key_overlap_ratio,
)


class _FakeClient:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows if rows is not None else [["100", "93"]]
        self.error = error
        self.queries: list[str] = []

    def execute_query(self, sql: str, **kwargs):
        self.queries.append(sql)
        if self.error:
            raise self.error
        return ["left_distinct", "resolved"], self.rows


_LEFT = "`cat`.`bronze`.`transactions`"
_RIGHT = "`cat`.`bronze`.`payors`"


class UCKeyOverlapTests(unittest.TestCase):
    def test_the_ratio_is_resolved_over_distinct_left_keys(self):
        client = _FakeClient(rows=[["100", "93"]])
        ratio = _uc_key_overlap_ratio(client, _LEFT, "PayorID", _RIGHT, "PayorID")
        self.assertAlmostEqual(ratio, 0.93)

    def test_the_work_happens_in_sql_and_returns_one_row(self):
        """The TB property: no value set crosses the wire."""
        client = _FakeClient()
        _uc_key_overlap_ratio(client, _LEFT, "PayorID", _RIGHT, "PayorID")
        sql = client.queries[0].upper()
        self.assertIn("DISTINCT", sql)
        self.assertIn("JOIN", sql)
        self.assertIn("COUNT(", sql)
        # A LIMIT would turn the ratio into a sample-vs-sample intersection,
        # which under-counts overlap and reports valid FKs as broken.
        self.assertNotIn("LIMIT", sql)

    def test_no_left_keys_is_unknown_not_zero(self):
        """Zero distinct left keys means nothing was measured. Returning 0.0
        would read as "no key resolves" and fail a join that is simply
        unmeasured -- the same absent-vs-zero contract the CSV path uses."""
        client = _FakeClient(rows=[["0", "0"]])
        self.assertIsNone(
            _uc_key_overlap_ratio(client, _LEFT, "PayorID", _RIGHT, "PayorID")
        )

    def test_a_query_failure_is_unknown_not_zero(self):
        client = _FakeClient(error=RuntimeError("warehouse unavailable"))
        self.assertIsNone(
            _uc_key_overlap_ratio(client, _LEFT, "PayorID", _RIGHT, "PayorID")
        )

    def test_a_hostile_column_name_is_refused_not_interpolated(self):
        """These identifiers reach a warehouse as text. A contract is a
        generated artifact, but it is not a trust boundary."""
        client = _FakeClient()
        self.assertIsNone(
            _uc_key_overlap_ratio(
                client, _LEFT, "PayorID; DROP TABLE x", _RIGHT, "PayorID"
            )
        )
        self.assertEqual(client.queries, [], "a hostile name reached the warehouse")

    def test_no_client_means_unknown(self):
        self.assertIsNone(
            _uc_key_overlap_ratio(None, _LEFT, "PayorID", _RIGHT, "PayorID")
        )


class ClientIsResolvedLazilyTests(unittest.TestCase):
    """`DatabricksClient.is_configured()` costs ~3s. Resolving a client for a
    pure-local workspace pays that for nothing -- measured at 20s across one
    test run before this was made lazy. The client must only be built when a
    UC-backed pair actually needs it."""

    def test_a_local_csv_pair_never_builds_a_client(self):
        built: list[int] = []

        def _factory():
            built.append(1)
            return _FakeClient()

        csv_profile = {"path": "workspaces/demo/datasets/a.csv", "format": "csv"}
        _left_key_resolution_ratio(
            csv_profile, "k", csv_profile, "k", None, _factory
        )
        self.assertEqual(built, [], "a client was built for a local CSV pair")

    def test_a_uc_pair_does_build_one(self):
        built: list[int] = []
        client = _FakeClient(rows=[["10", "10"]])

        def _factory():
            built.append(1)
            return client

        uc_profile = {"path": _LEFT, "format": "delta"}
        right = {"path": _RIGHT, "format": "delta"}
        ratio = _left_key_resolution_ratio(
            uc_profile, "PayorID", right, "PayorID", None, _factory
        )
        self.assertEqual(built, [1])
        self.assertEqual(ratio, 1.0)


if __name__ == "__main__":
    unittest.main()

"""`DatabricksClient.execute_query`'s read path, against a fake SDK seam.

Two production-review findings (P1/P2, docs/plans/rcm_replay_findings.md) live
here, and both are the shape that a green suite never catches because the only
caller today reads bounded metadata:

  P1 -- the method returned `resp.result.data_array`, which is the FIRST CHUNK
        of a paginated result. Nothing followed `next_chunk_index`, so a large
        read returned a prefix and reported success. A silent wrong answer is
        the worst failure a data platform can have.
  P2 -- the poll loop was `while state in (PENDING, RUNNING): sleep(2)` with no
        deadline and no retry, so a hung statement looped forever holding a
        worker slot, and one transient blip mid-poll abandoned a statement that
        was running fine server-side.

Nothing here touches the network: the SDK's statement_execution seam is faked,
and the real `StatementState` enum is used so the states are the ones the SDK
actually returns.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from databricks.sdk.service.sql import StatementState

from core.execution.databricks_client import DatabricksClient


def _resp(state, rows=None, next_chunk=None, columns=("c",), statement_id="s1"):
    result = (
        types.SimpleNamespace(data_array=rows, next_chunk_index=next_chunk)
        if rows is not None
        else None
    )
    return types.SimpleNamespace(
        statement_id=statement_id,
        status=types.SimpleNamespace(state=state, error=None),
        manifest=types.SimpleNamespace(
            schema=types.SimpleNamespace(
                columns=[types.SimpleNamespace(name=c) for c in columns]
            )
        ),
        result=result,
    )


class _FakeStatementExecution:
    def __init__(self, first, *, chunks=None, polls=None):
        self._first = first
        self._chunks = chunks or {}
        self._polls = list(polls or [])
        self.chunk_calls: list[int] = []
        self.poll_calls = 0

    def execute_statement(self, **kwargs):
        return self._first

    def get_statement(self, statement_id):
        self.poll_calls += 1
        nxt = self._polls.pop(0) if self._polls else None
        if isinstance(nxt, Exception):
            raise nxt
        return nxt if nxt is not None else self._first

    def get_statement_result_chunk_n(self, statement_id, chunk_index):
        self.chunk_calls.append(chunk_index)
        return self._chunks[chunk_index]


def _client(seam):
    client = DatabricksClient(cfg=mock.Mock())
    client.get_client = lambda: types.SimpleNamespace(statement_execution=seam)
    client._extract_warehouse_id = lambda: "wh1"
    return client


class ResultPaginationTests(unittest.TestCase):
    def test_every_chunk_is_read_not_only_the_first(self):
        seam = _FakeStatementExecution(
            _resp(StatementState.SUCCEEDED, rows=[["a"]], next_chunk=1),
            chunks={
                1: types.SimpleNamespace(data_array=[["b"]], next_chunk_index=2),
                2: types.SimpleNamespace(data_array=[["c"]], next_chunk_index=None),
            },
        )
        columns, rows = _client(seam).execute_query("select 1")
        self.assertEqual(columns, ["c"])
        self.assertEqual(rows, [["a"], ["b"], ["c"]])
        self.assertEqual(seam.chunk_calls, [1, 2])

    def test_a_single_chunk_result_fetches_no_extra_chunks(self):
        seam = _FakeStatementExecution(
            _resp(StatementState.SUCCEEDED, rows=[["a"]], next_chunk=None)
        )
        _, rows = _client(seam).execute_query("select 1")
        self.assertEqual(rows, [["a"]])
        self.assertEqual(seam.chunk_calls, [])

    def test_passing_the_row_cap_raises_instead_of_truncating(self):
        """The cap exists so an unbounded read fails loudly rather than
        exhausting memory -- but it must never become a silent prefix, which
        is the exact bug it is guarding."""
        seam = _FakeStatementExecution(
            _resp(StatementState.SUCCEEDED, rows=[["a"], ["b"]], next_chunk=1),
            chunks={1: types.SimpleNamespace(data_array=[["c"]], next_chunk_index=None)},
        )
        with self.assertRaises(RuntimeError) as caught:
            _client(seam).execute_query("select 1", max_rows=2)
        self.assertIn("max_rows", str(caught.exception))


class PollCeilingTests(unittest.TestCase):
    def test_a_statement_that_never_finishes_raises_at_the_deadline(self):
        seam = _FakeStatementExecution(_resp(StatementState.RUNNING))
        with mock.patch("time.sleep"):
            with self.assertRaises(TimeoutError) as caught:
                _client(seam).execute_query("select 1", timeout=0)
        self.assertIn("still", str(caught.exception).lower())

    def test_a_transient_poll_failure_is_retried_not_lost(self):
        """One blip against get_statement used to abandon a statement that was
        running fine server-side."""
        seam = _FakeStatementExecution(
            _resp(StatementState.RUNNING),
            polls=[
                ConnectionError("transient"),
                _resp(StatementState.SUCCEEDED, rows=[["a"]], next_chunk=None),
            ],
        )
        with mock.patch("time.sleep"):
            _, rows = _client(seam).execute_query("select 1")
        self.assertEqual(rows, [["a"]])

    def test_repeated_poll_failures_surface_the_error(self):
        seam = _FakeStatementExecution(
            _resp(StatementState.RUNNING),
            polls=[ConnectionError("down")] * 20,
        )
        with mock.patch("time.sleep"):
            with self.assertRaises(Exception):
                _client(seam).execute_query("select 1")


if __name__ == "__main__":
    unittest.main()

"""core.observability.warehouse_cost: Phase 3 read-back of warehouse spend.

`execute_query` is mocked -- this covers the gate, the SQL shape, the
allocation arithmetic and the cost-basis separation, not a live
`system.billing` round trip (which needs an admin-enabled system schema and a
human-set AUTORESEARCH_ALLOW_REMOTE_EXECUTION).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.observability.cost_ledger import CostLedger, build_anchor, ledger_dir_for
from core.observability.warehouse_cost import (
    COST_SOURCE,
    reconcile_warehouse_cost,
    warehouse_cost_sql,
)
from core.storage.workspace_layout import WorkspaceLayout

_AUTHORIZED_ENV = {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}
_RUN = "sess-abcd1234"

_COLUMNS = [
    "warehouse_id", "query_count", "run_duration_ms", "warehouse_duration_ms",
    "warehouse_dbus", "warehouse_usd", "run_usd",
]


def _workspace(root: Path) -> Path:
    ws = root / "workspaces" / "demo"
    WorkspaceLayout(project_root=ws).ensure_runtime_dirs()
    return ws


def _client(rows: list[list]) -> MagicMock:
    client = MagicMock()
    client.execute_query.return_value = (_COLUMNS, rows)
    return client


class SqlTests(unittest.TestCase):
    def test_an_unrecognised_run_id_is_refused_not_interpolated(self):
        # There is no bind-parameter path through execute_query, so anything
        # that is not one of our own minted ids must not reach the SQL string.
        for bad in ("'; DROP TABLE x --", "", "sess-XYZ", "../etc"):
            with self.subTest(run_id=bad):
                with self.assertRaises(ValueError):
                    warehouse_cost_sql(bad)

    def test_both_minted_run_id_shapes_are_accepted(self):
        self.assertIn(_RUN, warehouse_cost_sql(_RUN))
        self.assertIn("20260727T010203Z-deadbeef",
                      warehouse_cost_sql("20260727T010203Z-deadbeef"))

    def test_it_filters_on_the_run_tag_and_prices_from_list_prices(self):
        sql = warehouse_cost_sql(_RUN)
        self.assertIn("system.query.history", sql)
        self.assertIn("q.query_tags['run_id']", sql)
        self.assertIn("system.billing.usage", sql)
        self.assertIn("system.billing.list_prices", sql)
        self.assertIn("pricing.effective_list.default", sql)

    def test_it_allocates_by_share_rather_than_charging_the_whole_warehouse(self):
        sql = warehouse_cost_sql(_RUN)
        # The denominator CTE is what stops a run on a busy warehouse being
        # billed that warehouse's entire spend.
        self.assertIn("warehouse_duration_ms", sql)
        self.assertIn("NULLIF(w.warehouse_duration_ms, 0)", sql)


class RemoteGateTests(unittest.TestCase):
    def test_without_approval_nothing_is_queried_and_no_cost_is_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _workspace(root)
            client = _client([])
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", None)
                with self.assertRaises(PermissionError):
                    reconcile_warehouse_cost(root, "workspaces/demo",
                                             run_id=_RUN, client=client)
            client.execute_query.assert_not_called()
            report = json.loads(
                (root / "workspaces" / "demo" / "interns" / "reports" / "cost_ledger"
                 / "warehouse_cost.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "refused_no_remote_approval")
            self.assertEqual(report["warehouse_usd"], 0.0)
            # The refusal still shows the query, so it can be reviewed/run by hand.
            self.assertIn("system.billing.usage", report["sql"])


class ReconcileTests(unittest.TestCase):
    def _run(self, root: Path, rows: list[list]):
        with patch.dict(os.environ, _AUTHORIZED_ENV):
            return reconcile_warehouse_cost(
                root, "workspaces/demo", run_id=_RUN, client=_client(rows),
            )

    def test_rows_are_summed_into_a_run_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _workspace(root)
            result = self._run(root, [
                ["wh1", "10", "5000", "20000", "40", "8.0", "2.0"],
                ["wh2", "3", "1000", "1000", "5", "1.5", "1.5"],
            ])
            self.assertEqual(result.status, "reconciled")
            self.assertTrue(result.ok)
            self.assertAlmostEqual(result.warehouse_usd, 3.5)

    def test_a_missing_billing_row_is_zero_not_a_crash(self):
        # system.billing lags by hours; the LEFT JOIN returns nulls until it lands.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _workspace(root)
            result = self._run(root, [["wh1", "10", "5000", "20000", None, None, None]])
            self.assertEqual(result.status, "reconciled")
            self.assertEqual(result.warehouse_usd, 0.0)

    def test_no_tagged_queries_is_a_finding_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _workspace(root)
            result = self._run(root, [])
            self.assertEqual(result.status, "no_tagged_queries")
            self.assertFalse(result.ok)
            md = (root / result.current_markdown_path).read_text(encoding="utf-8")
            self.assertIn("AUTORESEARCH_RUN_ID", md)

    def test_warehouse_dollars_never_touch_the_agent_token_ledger(self):
        # The whole reason this is a separate artifact: AnchorEntry.cost_usd is
        # agent-token cost. Warehouse DBU dollars are a different basis.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _workspace(root)
            ledger = CostLedger(ledger_dir_for("workspaces/demo", root))
            ledger.append(build_anchor(
                run_id=_RUN, workspace_id="workspaces/demo", pipeline_stage="dbt_build",
                env={"CLAUDE_CODE_SESSION_ID": "uuid-1"},
            ))
            result = self._run(root, [["wh1", "10", "5000", "10000", "40", "8.0", "4.0"]])

            report = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(report["cost_source"], COST_SOURCE)
            self.assertEqual(report["anchor_rows_for_run"], 1)
            self.assertAlmostEqual(report["warehouse_usd"], 4.0)

            anchors = ledger.entries_for_run(_RUN)
            self.assertEqual(len(anchors), 1)
            self.assertIsNone(anchors[0]["cost_usd"])
            self.assertEqual(anchors[0]["cost_source"], "unreconciled")
            self.assertNotIn("warehouse_usd", anchors[0])

    def test_the_markdown_states_the_basis_and_that_it_is_an_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _workspace(root)
            result = self._run(root, [["wh1", "10", "5000", "10000", "40", "8.0", "4.0"]])
            md = (root / result.current_markdown_path).read_text(encoding="utf-8")
            self.assertIn(COST_SOURCE, md)
            self.assertIn("allocation", md)
            self.assertIn("NOT agent-token cost", md)


class ResultPacketTests(unittest.TestCase):
    """3.4: the packet must never render a missing cost as a zero."""

    def _render(self, run_cost):
        from core.onboarding.workspace.flow_panels import _render_results_markdown

        return _render_results_markdown({"workspace": "workspaces/demo", "kpis": [],
                                         "run_cost": run_cost})

    def test_absent_reconciliation_says_so_and_names_the_command(self):
        md = self._render(None)
        self.assertIn("not reconciled", md)
        self.assertIn("reconcile-warehouse-cost", md)
        self.assertNotIn("$0.00", md)

    def test_a_reconciled_run_renders_the_figure_and_its_basis(self):
        md = self._render({
            "status": "reconciled", "run_id": _RUN, "warehouse_usd": 4.0,
            "warehouse_dbus": 40.0, "cost_source": COST_SOURCE,
        })
        self.assertIn("$4.00", md)
        self.assertIn(COST_SOURCE, md)
        self.assertIn("NOT included", md)

    def test_a_refused_reconciliation_is_not_a_zero_either(self):
        md = self._render({"status": "refused_no_remote_approval",
                           "reason": "AUTORESEARCH_ALLOW_REMOTE_EXECUTION is not 1"})
        self.assertIn("not available", md)
        self.assertIn("AUTORESEARCH_ALLOW_REMOTE_EXECUTION", md)
        self.assertNotIn("$0.00", md)

    def test_another_runs_reconciliation_is_never_shown_as_this_runs(self):
        md = self._render({"status": "stale", "reason": "the reconciliation on disk "
                                                        "is for run `sess-00000000`"})
        self.assertIn("stale", md)
        self.assertNotIn("$", md)


if __name__ == "__main__":
    unittest.main()

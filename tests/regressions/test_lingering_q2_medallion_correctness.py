"""Regressions for Q2 (lingering-issues plan): medallion build/design
correctness. See ~/.claude/plans/dynamic-cooking-firefly.md Q2.

- KPI regen no longer fabricates "equal" by byte-copying v1 into v2.
- An assertion-execution failure still surfaces as a real FAIL (verified as
  already-correct against current code; locked here so it stays that way).
- Silver `null_policies` (drop/error/default) are actually enforced in the
  emitted SQL, not TODO placeholders -- verified by executing the generated
  SQL against a real DuckDB connection.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from core.medallion.build import _NullGovernor, _execute_kpi_regen, _run_assertions, _split_statements
from core.medallion.design import _emit_silver_sql_duckdb, _render_assertions_sql
from core.medallion.manifest import Manifest, SilverTable
from core.medallion.run_state import RunState
from core.medallion.silver_contract import NullPolicy, SilverContract, TableContract, TypeCast
from core.storage.workspace_layout import WorkspaceLayout


def _run_state() -> RunState:
    now = datetime.now(timezone.utc).isoformat()
    return RunState(
        run_id="test-run", manifest_hash="sha256:test",
        target_declared="duckdb", target_actual="duckdb", started_at=now,
    )


class KpiRegenNoFabricationTests(unittest.TestCase):
    """A missing v2 (Gold-regenerated bundle) must never be silently
    manufactured by copying v1 -- that always compares "equal" to itself and
    proves nothing."""

    def _run(self, workspace: Path, repo_root: Path) -> RunState:
        layout = WorkspaceLayout(project_root=workspace)
        layout.ensure_runtime_dirs()
        (layout.contracts_dir / "kpi_registry.json").write_text("{}", encoding="utf-8")
        manifest = Manifest(workspace="demo", inputs_hash="sha256:test")
        con = duckdb.connect(":memory:")
        state = _run_state()
        _execute_kpi_regen(manifest, layout, {}, con, state, governor=_NullGovernor(), workspace=workspace, repo_root=repo_root)
        con.close()
        return state

    def test_missing_v2_is_skipped_and_never_created_by_copying_v1(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspaces" / "demo"
            v1 = workspace / "interns" / "generated" / "solutions" / "kpi_metrics.sql"
            v1.parent.mkdir(parents=True, exist_ok=True)
            v1.write_text("-- KPI: revenue\nSELECT 1 AS revenue;\n", encoding="utf-8")
            v2 = workspace / "interns" / "generated" / "solutions" / "kpi_metrics_v2.sql"
            state = self._run(workspace, repo_root)
            self.assertFalse(v2.exists(), "v2 must never be fabricated by copying v1")
            self.assertEqual(state.kpi_diff, {})

    def test_baseline_manifest_with_no_kpi_anchors_is_skipped(self):
        # kpi_metrics.sql as onboarding actually writes it: a VALUES manifest,
        # zero '-- KPI:' anchors. Even with a (also anchor-less) v2 present,
        # there is nothing comparable -- must not report an empty "all equal" diff.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspaces" / "demo"
            solutions = workspace / "interns" / "generated" / "solutions"
            solutions.mkdir(parents=True, exist_ok=True)
            manifest_sql = (
                "-- Generated baseline KPI manifest.\n"
                "CREATE OR REPLACE TABLE kpi_baseline_manifest AS\n"
                "SELECT * FROM (VALUES (1, 'Revenue', 'sum(amount)', '', 'ready')) "
                "AS t(kpi_id, kpi_name, metric_expression, grain_or_cuts, status);\n"
            )
            (solutions / "kpi_metrics.sql").write_text(manifest_sql, encoding="utf-8")
            (solutions / "kpi_metrics_v2.sql").write_text(manifest_sql, encoding="utf-8")
            state = self._run(workspace, repo_root)
            self.assertEqual(state.kpi_diff, {})

    def test_real_kpi_anchored_mismatch_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspaces" / "demo"
            solutions = workspace / "interns" / "generated" / "solutions"
            solutions.mkdir(parents=True, exist_ok=True)
            (solutions / "kpi_metrics.sql").write_text(
                "-- KPI: revenue\nSELECT 1 AS revenue;\n", encoding="utf-8",
            )
            (solutions / "kpi_metrics_v2.sql").write_text(
                "-- KPI: revenue\nSELECT 2 AS revenue;\n", encoding="utf-8",
            )
            state = self._run(workspace, repo_root)
            self.assertIn("revenue", state.kpi_diff)
            self.assertFalse(state.kpi_diff["revenue"]["equal"])


class AssertionExecutionFailureTests(unittest.TestCase):
    """A SQL error while running an assertion statement must surface as a
    real failure, never a silent pass."""

    def test_execution_failure_is_a_fail_not_a_pass(self):
        con = duckdb.connect(":memory:")
        con.execute("CREATE TABLE t(id INTEGER)")
        # Second statement references a column that does not exist -> raises.
        violations = _run_assertions(con, "SELECT 'a1', COUNT(*) FROM t; SELECT 'a2' FROM t WHERE nope IS NULL;")
        con.close()
        assertion_results = {
            aid: ("pass" if count == 0 else f"FAIL:{count}") for aid, count in violations.items()
        }
        failing = {k: v for k, v in assertion_results.items() if v.startswith("FAIL")}
        self.assertTrue(failing, "an execution failure on the 2nd statement must be flagged")


class SilverNullPolicyEnforcementTests(unittest.TestCase):
    """null_policies (drop/error/default) must be real, executable SQL --
    proven by actually running the emitted Silver SQL."""

    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        self.con.execute("CREATE SCHEMA IF NOT EXISTS silver")

    def tearDown(self):
        self.con.close()

    def _emit_and_load(self, table: str, tc: TableContract, bronze_rows: list[tuple]) -> str:
        values_sql = ", ".join(f"({r[0]}, {r[1]})" for r in bronze_rows)
        self.con.execute(f"CREATE TABLE bronze.{table} AS SELECT * FROM (VALUES {values_sql}) AS t(id, val)")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            manifest = Manifest(
                workspace="demo", inputs_hash="sha256:test",
                silver=[SilverTable(name=table, derived_from=[f"bronze.{table}"], primary_key=["id"])],
            )
            contract = SilverContract(workspace="demo", tables={table: tc})
            [sql_path] = _emit_silver_sql_duckdb(manifest, contract, out_dir)
            sql_text = sql_path.read_text(encoding="utf-8")
            # Everything up to the assertions-file pointer comment is the
            # actual CREATE OR REPLACE TABLE statement.
            self.con.execute(sql_text.split("-- Post-load assertions")[0])
            return _render_assertions_sql(table, tc)

    def test_drop_policy_removes_null_rows(self):
        tc = TableContract(null_policies={"val": NullPolicy("drop")})
        self._emit_and_load("t_drop", tc, [(1, "'a'"), (2, "NULL")])
        rows = self.con.execute("SELECT id FROM silver.t_drop").fetchall()
        self.assertEqual([r[0] for r in rows], [1])

    def test_default_policy_coalesces_nulls_numeric(self):
        tc = TableContract(null_policies={"val": NullPolicy("default:0")})
        self._emit_and_load("t_default_num", tc, [(1, "5"), (2, "NULL")])
        rows = dict(self.con.execute("SELECT id, val FROM silver.t_default_num ORDER BY id").fetchall())
        self.assertEqual(rows, {1: 5, 2: 0})

    def test_default_policy_coalesces_nulls_string_and_composes_with_cast(self):
        tc = TableContract(
            type_casts={"val": TypeCast(from_type="VARCHAR", to_type="VARCHAR")},
            null_policies={"val": NullPolicy("default:Unknown")},
        )
        self._emit_and_load("t_default_str", tc, [(1, "'active'"), (2, "NULL")])
        rows = dict(self.con.execute("SELECT id, val FROM silver.t_default_str ORDER BY id").fetchall())
        self.assertEqual(rows, {1: "active", 2: "Unknown"})

    def test_error_policy_emits_a_failing_not_null_assertion(self):
        tc = TableContract(null_policies={"val": NullPolicy("error")})
        assertions_sql = self._emit_and_load("t_error", tc, [(1, "'a'"), (2, "NULL")])
        violations = {}
        for stmt in _split_statements(assertions_sql):
            aid, count = self.con.execute(stmt).fetchone()
            violations[aid] = count
        self.assertEqual(violations.get("null_policy_error_val"), 1)


if __name__ == "__main__":
    unittest.main()

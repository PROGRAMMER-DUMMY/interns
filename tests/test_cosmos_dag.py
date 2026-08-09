"""core.orchestration.cosmos_dag: Cosmos wiring for the dbt_build stage.

Airflow/Cosmos are optional deps not installed in this environment (matches
airflow_dag.py's own design) -- these tests cover the graceful-without-it
behavior and the pure logic (_read_profile_name, workspace validation)
directly, the same bar test_pipeline_orchestration.py's AirflowWiringTests
already established for the sibling module.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.orchestration import cosmos_dag

# build_dbt_tasks() now checks the remote-execution gate first (Security S1) --
# tests below that exercise OTHER failure modes (missing cosmos, empty
# workspace) authorize remote execution explicitly so they keep testing what
# they always tested, isolated from the new gate's own state.
_AUTHORIZED_ENV = {"AUTORESEARCH_ALLOW_REMOTE_EXECUTION": "1"}


class CosmosAvailableTests(unittest.TestCase):
    def test_reports_false_when_cosmos_not_installed(self):
        try:
            import cosmos  # noqa: F401
        except ImportError:
            self.assertFalse(cosmos_dag.cosmos_available())
        else:
            self.skipTest("astronomer-cosmos is installed in this environment")


class BuildDbtTasksTests(unittest.TestCase):
    def test_requires_cosmos_clearly(self):
        try:
            import cosmos  # noqa: F401
        except ImportError:
            with mock.patch.dict(os.environ, _AUTHORIZED_ENV):
                with self.assertRaises(SystemExit):
                    cosmos_dag.build_dbt_tasks(workspace="workspaces/demo", repo_root=".")
        else:
            self.skipTest("astronomer-cosmos is installed in this environment")

    def test_empty_workspace_rejected_before_touching_cosmos(self):
        # The ${WORKSPACE}-deferred placeholder mode airflow_dag.py's plain
        # BashOperator path supports has no Cosmos equivalent -- a concrete
        # workspace is required at DAG-parse time. This must fail with a
        # clear ValueError, not silently build a broken operator (and must
        # fail on the empty-workspace check specifically, not by masking it
        # behind ImportError when Cosmos also happens to be missing, or by
        # the remote-execution gate -- authorized explicitly here so this
        # test still isolates the workspace check).
        try:
            import cosmos  # noqa: F401
        except ImportError:
            self.skipTest("cannot isolate ValueError from ImportError without cosmos installed")
        with mock.patch.dict(os.environ, _AUTHORIZED_ENV):
            with self.assertRaises(ValueError):
                cosmos_dag.build_dbt_tasks(workspace="", repo_root=".")


class GhostReconcileQualifiesTableNamesTests(unittest.TestCase):
    """F20: `reconcile_ghost_tables` now diffs on dbt's fully-qualified
    `relation_name` (Task C3), but this caller selected only `table_name` from
    information_schema and passed BARE names in. On the qualified path nothing
    would match and every live table would report as an orphan -- the exact
    inversion of the bug C3 fixed. C3's own tests missed it because they
    exercise the function in isolation with already-qualified input."""

    def test_live_tables_are_passed_fully_qualified(self):
        seen: dict[str, list[str]] = {}

        def _fake_reconcile(dbt_dir, warehouse_tables, **kwargs):
            seen["tables"] = list(warehouse_tables)
            return "reconciled"

        class _FakeClient:
            def __init__(self, cfg):
                pass

            def execute_query(self, sql, **kwargs):
                return ["table_name"], [["fct_x"], ["fct_orphan"]]

        class _ActiveCfg:
            def is_active(self):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_rel = "workspaces/demo"
            (root / ws_rel / "interns").mkdir(parents=True)
            (root / ws_rel / "workspace_settings.json").write_text(
                '{"databricks_source": {"catalog": "cat", "enterprise_id": ""}}',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, _AUTHORIZED_ENV), \
                mock.patch(
                    "core.onboarding.kpi.dbt_project_generator.reconcile_ghost_tables",
                    _fake_reconcile,
                ), \
                mock.patch(
                    "core.config.resolve_databricks_config",
                    lambda _eid: _ActiveCfg(),
                ), \
                mock.patch(
                    "core.execution.databricks_client.DatabricksClient", _FakeClient
                ):
                cosmos_dag._run_ghost_reconcile(
                    workspace=ws_rel, repo_root=str(root), schema="gold"
                )

        self.assertEqual(seen["tables"], ["cat.gold.fct_x", "cat.gold.fct_orphan"])

    def test_the_provisioned_catalog_wins_over_the_declared_base(self):
        """F21: `databricks_source.catalog` is the base an operator declared;
        provisioning creates `<base>_<env>`. Querying the base's
        information_schema hits a catalog that does not exist -- an empty
        listing, which this function treats as "no live tables" and which then
        reports every project model as an orphan."""
        seen: dict[str, list[str]] = {}
        queried: list[str] = []

        def _fake_reconcile(dbt_dir, warehouse_tables, **kwargs):
            seen["tables"] = list(warehouse_tables)
            return "reconciled"

        class _FakeClient:
            def __init__(self, cfg):
                pass

            def execute_query(self, sql, **kwargs):
                queried.append(sql)
                return ["table_name"], [["fct_x"]]

        class _ActiveCfg:
            def is_active(self):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_rel = "workspaces/demo"
            (root / ws_rel / "interns" / "generated" / "contracts").mkdir(parents=True)
            (root / ws_rel / "workspace_settings.json").write_text(
                '{"databricks_source": {"catalog": "cat", "enterprise_id": ""}}',
                encoding="utf-8",
            )
            (root / ws_rel / "interns" / "generated" / "contracts" / "provision_plan.json").write_text(
                '{"catalog": "cat_dev", "catalog_base": "cat", "env": "dev"}',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, _AUTHORIZED_ENV), \
                mock.patch(
                    "core.onboarding.kpi.dbt_project_generator.reconcile_ghost_tables",
                    _fake_reconcile,
                ), \
                mock.patch(
                    "core.config.resolve_databricks_config",
                    lambda _eid: _ActiveCfg(),
                ), \
                mock.patch(
                    "core.execution.databricks_client.DatabricksClient", _FakeClient
                ):
                cosmos_dag._run_ghost_reconcile(
                    workspace=ws_rel, repo_root=str(root), schema="gold"
                )

        self.assertIn("`cat_dev`.information_schema.tables", queried[0])
        self.assertEqual(seen["tables"], ["cat_dev.gold.fct_x"])


class DbtTargetMatchesProvisionedEnvTests(unittest.TestCase):
    """F26: the Cosmos task hardcoded `target_name="prod"`. Since C4 split
    profiles.yml into dev/prod on `<base>_dev`/`<base>_prod`, and provisioning
    defaults to `--env dev` (so only `<base>_dev` is ever created), the
    orchestrated `dbt build` resolved to a catalog that does not exist. Sources
    follow `{{ target.database }}`, so it would read an empty/absent catalog --
    the DAG runs against nothing. No test could see it: Cosmos is not installed
    here, so the operator is never constructed."""

    def _ws(self, root: Path, plan: dict | None) -> str:
        ws_rel = "workspaces/demo"
        (root / ws_rel / "interns" / "generated" / "contracts").mkdir(parents=True)
        if plan is not None:
            (root / ws_rel / "interns" / "generated" / "contracts" / "provision_plan.json").write_text(
                json.dumps(plan), encoding="utf-8"
            )
        return ws_rel

    def test_target_is_the_provisioned_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self._ws(root, {"catalog": "demo_dev", "catalog_base": "demo", "env": "dev"})
            self.assertEqual(cosmos_dag._dbt_target_name(str(root), ws), "dev")

    def test_unprovisioned_workspace_keeps_prod(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self._ws(root, None)
            self.assertEqual(cosmos_dag._dbt_target_name(str(root), ws), "prod")

    def test_the_operator_actually_uses_it(self):
        # The seam every other test here cannot reach, because Cosmos is not
        # installed: assert on the source that build_dbt_tasks passes the
        # resolved target rather than a literal. (The F19/F20 lesson.)
        import inspect

        source = inspect.getsource(cosmos_dag.build_dbt_tasks)
        self.assertIn("_dbt_target_name", source)
        self.assertNotIn('target_name="prod"', source)


class ReadProfileNameTests(unittest.TestCase):
    def test_reads_the_profile_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = Path(tmp) / "dbt"
            dbt_dir.mkdir()
            (dbt_dir / "dbt_project.yml").write_text(
                "name: 'demo'\nprofile: 'dbx_demo3'\nversion: '1.0.0'\n",
                encoding="utf-8",
            )
            self.assertEqual(cosmos_dag._read_profile_name(dbt_dir), "dbx_demo3")

    def test_missing_project_yml_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = Path(tmp) / "dbt"
            dbt_dir.mkdir()
            with self.assertRaises(FileNotFoundError):
                cosmos_dag._read_profile_name(dbt_dir)

    def test_missing_profile_key_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = Path(tmp) / "dbt"
            dbt_dir.mkdir()
            (dbt_dir / "dbt_project.yml").write_text("name: 'demo'\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                cosmos_dag._read_profile_name(dbt_dir)


if __name__ == "__main__":
    unittest.main()

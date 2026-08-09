"""Emitted-code invariants for the generated dbt project (cloud-first
restructure spec section 6).

Every rule asserted here guards a path dbt-databricks fails SILENTLY on --
merge-without-unique_key degrading to append, `on_schema_change` defaulting to
'ignore' and dropping columns, replace_where inserting by position, an
identity surrogate key renumbering on rebuild. The assertions are on the
emitted TEXT, because that is what the warehouse actually runs.
"""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from core.onboarding.kpi import dbt_project_generator
from core.onboarding.kpi.dbt_project_generator import (
    DbtProjectGenerator,
    reconcile_ghost_tables,
    resolve_catalog_and_base,
    validate_generated_project,
)
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.storage.workspace_layout import WorkspaceLayout

_RETENTION = (
    "tblproperties={'delta.deletedFileRetentionDuration': 'interval 730 days', "
    "'delta.logRetentionDuration': 'interval 730 days'}"
)


def _generate(root: Path) -> Path:
    """A real generated project for the same one-KPI CSV workspace
    test_dbt_project_generator.py uses. Returns the dbt project dir."""
    workspace = root / "workspaces" / "demo"
    (workspace / "datasets").mkdir(parents=True)
    (workspace / "docs").mkdir(parents=True)
    (workspace / "datasets" / "transactions.csv").write_text(
        "ClaimID,PaidAmount,LineOfBusiness\nC1,10.50,Commercial\nC2,20.25,Medicare\n",
        encoding="utf-8",
    )
    (workspace / "docs" / "kpi_registry.csv").write_text(
        "Key business question,Description,Cuts / grain hints,Metric,"
        "Data model refinement required\n"
        "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,"
        "sum(PaidAmount),\n",
        encoding="utf-8",
    )
    WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
    KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
    result = DbtProjectGenerator(
        root, "workspaces/demo", catalog="main", schema="rcm"
    ).generate()
    return root / result.dbt_project_dir


def _temporal_workspace(root: Path, *, lane: str = "", late_dims: bool = False) -> Path:
    """The same one-KPI CSV workspace, but with a temporal grain (Month of a
    date column) and an optional velocity lane / modeling decision from the
    blueprint -- the inputs that decide an incremental mart."""
    workspace = root / "workspaces" / "demo"
    (workspace / "datasets").mkdir(parents=True)
    (workspace / "docs").mkdir(parents=True)
    (workspace / "datasets" / "transactions.csv").write_text(
        "ClaimID,PaidAmount,LineOfBusiness,ServiceDate\n"
        "C1,10.50,Commercial,2024-01-05\nC2,20.25,Medicare,2024-02-11\n",
        encoding="utf-8",
    )
    (workspace / "docs" / "kpi_registry.csv").write_text(
        "Key business question,Description,Cuts / grain hints,Metric,"
        "Data model refinement required\n"
        "What is paid amount by line of business over time?,Baseline KPI,"
        "Month(ServiceDate); LineOfBusiness,sum(PaidAmount),\n",
        encoding="utf-8",
    )
    WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
    KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
    decisions: list[dict] = []
    if lane:
        decisions.append({"decision": "velocity_lane", "choice": lane, "status": "decided"})
    if late_dims:
        decisions.append(
            {"decision": "modeling", "choice": "inferred_member_dimensions", "status": "decided"}
        )
    if decisions:
        layout = WorkspaceLayout(project_root=workspace)
        (layout.contracts_dir / "blueprint_decisions.json").write_text(
            json.dumps({"decisions": decisions}), encoding="utf-8"
        )
    return workspace


def _generate_temporal(root: Path, **kwargs) -> Path:
    _temporal_workspace(root, **kwargs)
    result = DbtProjectGenerator(
        root, "workspaces/demo", catalog="main", schema="rcm"
    ).generate()
    return root / result.dbt_project_dir


class CatalogBaseResolutionTests(unittest.TestCase):
    """F21: `workspace_settings.databricks_source.catalog` is the catalog BASE
    the operator declared at intake; provisioning creates `env_catalog(base,
    env)`. Reading the base as if it were the concrete catalog points the whole
    generated project at a catalog that was never created. The provisioning
    plan is the authority -- it records both names."""

    def _layout(self, root: Path, *, plan: dict | None, declared: str) -> WorkspaceLayout:
        ws = root / "workspaces" / "demo"
        (ws / "interns" / "generated" / "contracts").mkdir(parents=True)
        (ws / "workspace_settings.json").write_text(
            json.dumps({"databricks_source": {"catalog": declared, "schema": "raw"}}),
            encoding="utf-8",
        )
        if plan is not None:
            (ws / "interns" / "generated" / "contracts" / "provision_plan.json").write_text(
                json.dumps(plan), encoding="utf-8"
            )
        return WorkspaceLayout(project_root=ws)

    def test_provision_plan_supplies_the_concrete_catalog_and_its_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(
                Path(tmp),
                plan={"catalog": "demo_dev", "catalog_base": "demo", "env": "dev"},
                declared="demo",
            )
            self.assertEqual(resolve_catalog_and_base(layout, ""), ("demo_dev", "demo"))

    def test_explicit_catalog_wins_and_is_its_own_base(self):
        # An operator naming a catalog outright means that exact catalog; the
        # env suffix is not re-applied on top of it.
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(
                Path(tmp),
                plan={"catalog": "demo_dev", "catalog_base": "demo", "env": "dev"},
                declared="demo",
            )
            self.assertEqual(resolve_catalog_and_base(layout, "other"), ("other", "other"))

    def test_without_a_plan_the_declared_value_is_used_unchanged(self):
        # Local/POC workspaces never run provisioning. Their behavior must not
        # change: no suffix is invented for them.
        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(Path(tmp), plan=None, declared="main")
            self.assertEqual(resolve_catalog_and_base(layout, ""), ("main", "main"))


class EmittedProjectInvariantTests(unittest.TestCase):
    def test_generated_project_passes_its_own_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate(Path(tmp))
            self.assertEqual(validate_generated_project(dbt_dir), [])

    def test_mart_table_declares_liquid_clustering_within_the_key_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate(Path(tmp))
            mart = (dbt_dir / "models" / "marts" / "fct_kpi_001.sql").read_text(
                encoding="utf-8"
            )
            self.assertIn("cluster_by=['lineofbusiness']", mart)
            self.assertNotIn("partition_by", mart)

    def test_surrogate_key_macro_is_a_deterministic_hash_not_an_identity_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate(Path(tmp))
            macro = (dbt_dir / "macros" / "surrogate_key.sql").read_text(encoding="utf-8")
            self.assertIn("md5(", macro)
            self.assertNotIn("IDENTITY", macro.upper())
            self.assertNotIn("monotonically_increasing_id", macro)

    def test_profiles_declare_query_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate(Path(tmp))
            self.assertIn(
                "query_tags", (dbt_dir / "profiles.yml").read_text(encoding="utf-8")
            )

    def test_profiles_declare_a_dev_and_a_prod_target_on_separate_catalogs(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate(Path(tmp))
            profiles = (dbt_dir / "profiles.yml").read_text(encoding="utf-8")
            self.assertIn("dev:", profiles)
            self.assertIn("prod:", profiles)
            self.assertIn("catalog: main_dev", profiles)
            self.assertIn("catalog: main_prod", profiles)

    def test_nothing_inside_the_project_hardcodes_one_targets_catalog(self):
        """F21: profiles.yml now names TWO catalogs (`<base>_dev`/`<base>_prod`),
        so any other emitted file that hardcodes a single catalog literal
        contradicts at least one of them -- and `sources.yml` hardcoded the
        un-suffixed base, a catalog provisioning never creates. Everything
        that runs INSIDE dbt must follow the active target instead."""
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate(Path(tmp))
            sources = (dbt_dir / "models" / "sources.yml").read_text(encoding="utf-8")
            self.assertIn("database: \"{{ target.database }}\"", sources)
            self.assertNotIn("database: main", sources)
            macro = (dbt_dir / "macros" / "publish_gold.sql").read_text(encoding="utf-8")
            self.assertIn("target.database", macro)
            self.assertNotIn("var('catalog')", macro)

    def test_dbt_project_declares_a_require_dbt_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate(Path(tmp))
            project = (dbt_dir / "dbt_project.yml").read_text(encoding="utf-8")
            self.assertIn("require-dbt-version:", project)

    def test_wap_marts_build_to_staging_and_publish_replaces_only_its_own_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate(Path(tmp))
            project = (dbt_dir / "dbt_project.yml").read_text(encoding="utf-8")
            self.assertIn("+schema: gold__staging", project)
            self.assertIn("gold_schema: gold", project)  # live schema still declared
            macro = (dbt_dir / "macros" / "publish_gold.sql").read_text(encoding="utf-8")
            # The swap is an explicit literal list of the marts this generator
            # wrote -- never a wildcard over the schema.
            self.assertIn("set marts = ['fct_kpi_001']", macro)
            self.assertIn("CREATE OR REPLACE TABLE", macro)
            self.assertIn("gold_staging_schema", macro)
            self.assertNotIn("DROP", macro.upper())


class IncrementalMartTests(unittest.TestCase):
    """Audit A1: marts were ALWAYS `table` and the incremental validators were
    dead code. A micro-batch/streaming lane with a temporal anchor now emits a
    real incremental merge mart -- which is what makes those validators live."""

    def test_batch_lane_keeps_a_full_refresh_table_mart(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate_temporal(Path(tmp), lane="batch")
            mart = (dbt_dir / "models" / "marts" / "fct_kpi_001.sql").read_text(encoding="utf-8")
            self.assertIn("materialized='table'", mart)
            self.assertNotIn("incremental", mart)

    def test_no_blueprint_contract_at_all_keeps_current_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate_temporal(Path(tmp))  # no blueprint_decisions.json
            mart = (dbt_dir / "models" / "marts" / "fct_kpi_001.sql").read_text(encoding="utf-8")
            self.assertIn("materialized='table'", mart)

    def test_micro_batch_lane_with_temporal_anchor_emits_incremental_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate_temporal(Path(tmp), lane="micro_batch")
            mart = (dbt_dir / "models" / "marts" / "fct_kpi_001.sql").read_text(encoding="utf-8")
            self.assertIn("materialized='incremental'", mart)
            self.assertIn("incremental_strategy='merge'", mart)
            self.assertIn("unique_key='kpi_001_sk'", mart)
            self.assertIn("on_schema_change='append_new_columns'", mart)
            self.assertIn("event_time='month'", mart)
            self.assertIn("lookback=", mart)
            # The merge key is the deterministic hash surrogate key of the
            # grain, never an identity/row-number.
            self.assertIn("{{ surrogate_key(", mart)
            self.assertIn("is_incremental()", mart)
            # The parent must declare event_time too, or dbt cannot bound an
            # event-time backfill/batch read of it.
            parent = (dbt_dir / "models" / "intermediate" / "int_kpi_001_features.sql").read_text(
                encoding="utf-8"
            )
            self.assertIn("event_time='ServiceDate'", parent)
            self.assertEqual(validate_generated_project(dbt_dir), [])

    def test_streaming_lane_without_a_temporal_anchor_stays_a_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            (workspace / "datasets").mkdir(parents=True)
            (workspace / "docs").mkdir(parents=True)
            (workspace / "datasets" / "transactions.csv").write_text(
                "ClaimID,PaidAmount,LineOfBusiness\nC1,10.50,Commercial\nC2,20.25,Medicare\n",
                encoding="utf-8",
            )
            (workspace / "docs" / "kpi_registry.csv").write_text(
                "Key business question,Description,Cuts / grain hints,Metric,"
                "Data model refinement required\n"
                "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,"
                "sum(PaidAmount),\n",
                encoding="utf-8",
            )
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            layout = WorkspaceLayout(project_root=workspace)
            (layout.contracts_dir / "blueprint_decisions.json").write_text(
                json.dumps({"decisions": [{"decision": "velocity_lane", "choice": "streaming"}]}),
                encoding="utf-8",
            )
            result = DbtProjectGenerator(
                root, "workspaces/demo", catalog="main", schema="rcm"
            ).generate()
            mart = (
                root / result.dbt_project_dir / "models" / "marts" / "fct_kpi_001.sql"
            ).read_text(encoding="utf-8")
            self.assertIn("materialized='table'", mart)

    def test_generation_refuses_when_its_own_incremental_omits_unique_key(self):
        """Mutation test: break the generator's own emission and the project
        must REFUSE to be declared usable -- the validator is on a reachable
        path now, not decoration."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _temporal_workspace(root, lane="micro_batch")
            broken = lambda unique_key, event_time, lookback: {  # noqa: E731
                "materialized": "incremental",
                "incremental_strategy": "merge",
                "on_schema_change": "append_new_columns",
                "event_time": event_time,
                "lookback": lookback,
            }
            with patch.object(dbt_project_generator, "_incremental_config", broken):
                with self.assertRaises(ValueError) as ctx:
                    DbtProjectGenerator(
                        root, "workspaces/demo", catalog="main", schema="rcm"
                    ).generate()
            self.assertIn("unique_key", str(ctx.exception))

    def test_publish_is_a_metadata_swap_for_incremental_marts(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate_temporal(Path(tmp), lane="micro_batch")
            macro = (dbt_dir / "macros" / "publish_gold.sql").read_text(encoding="utf-8")
            self.assertIn("set incremental_marts = ['fct_kpi_001']", macro)
            self.assertIn("set marts = []", macro)  # nothing full-copied
            self.assertIn("SHALLOW CLONE", macro)
            swap = macro.split("for mart in incremental_marts", 1)[1]
            self.assertNotIn("AS SELECT", swap.upper())
            self.assertNotIn("DROP", macro.upper())


class RetentionAndFreshnessTests(unittest.TestCase):
    def test_marts_declare_delta_retention_tblproperties(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate(Path(tmp))
            mart = (dbt_dir / "models" / "marts" / "fct_kpi_001.sql").read_text(encoding="utf-8")
            self.assertIn("delta.deletedFileRetentionDuration': 'interval 730 days'", mart)
            self.assertIn("delta.logRetentionDuration': 'interval 730 days'", mart)

    def test_table_without_retention_tblproperties_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = Path(tmp) / "dbt"
            (dbt_dir / "models" / "marts").mkdir(parents=True)
            (dbt_dir / "models" / "marts" / "fct_x.sql").write_text(
                "{{ config(materialized='table', cluster_by=['a']) }}\nselect 1 as a\n",
                encoding="utf-8",
            )
            self.assertTrue(
                any("retention" in p for p in validate_generated_project(dbt_dir)),
                validate_generated_project(dbt_dir),
            )

    def test_source_with_a_temporal_anchor_declares_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _generate_temporal(Path(tmp))
            sources = (dbt_dir / "models" / "sources.yml").read_text(encoding="utf-8")
            self.assertIn("loaded_at_field:", sources)
            self.assertIn("warn_after: {count: 2, period: day}", sources)
            self.assertIn("error_after: {count: 3, period: day}", sources)
            self.assertEqual(validate_generated_project(dbt_dir), [])

    def test_source_without_a_temporal_anchor_records_an_open_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dbt_dir = _generate(root)  # no date column anywhere
            sources = (dbt_dir / "models" / "sources.yml").read_text(encoding="utf-8")
            self.assertNotIn("freshness:", sources)
            self.assertIn("OPEN QUESTION", sources)
            report = (
                root / "workspaces" / "demo" / "interns" / "reports" / "dbt_generation"
                / "current.md"
            ).read_text(encoding="utf-8")
            self.assertIn("transactions", report)
            self.assertIn("loaded_at_field", report)
            # Silence is what the audit flagged; an explicit open question is
            # not a violation.
            self.assertEqual(validate_generated_project(dbt_dir), [])

    def test_source_with_neither_freshness_nor_open_question_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = Path(tmp) / "dbt"
            (dbt_dir / "models").mkdir(parents=True)
            (dbt_dir / "models" / "sources.yml").write_text(
                "version: 2\n\nsources:\n  - name: raw\n    database: main\n"
                "    schema: rcm\n    tables:\n      - name: transactions\n",
                encoding="utf-8",
            )
            self.assertTrue(
                any("freshness" in p for p in validate_generated_project(dbt_dir))
            )


class ValidatorRefusalTests(unittest.TestCase):
    """The validator on models the generator does not emit today but a later
    slice might -- this is the guard that keeps them honest."""

    def _project(self, tmp: str, model_sql: str, name: str = "fct_x") -> Path:
        dbt_dir = Path(tmp) / "dbt"
        (dbt_dir / "models" / "marts").mkdir(parents=True)
        (dbt_dir / "models" / "marts" / f"{name}.sql").write_text(
            model_sql, encoding="utf-8"
        )
        return dbt_dir

    def test_merge_without_unique_key_is_refused_naming_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = self._project(
                tmp,
                "{{ config(materialized='incremental', incremental_strategy='merge', "
                "on_schema_change='append_new_columns') }}\nselect 1 as a\n",
            )
            problems = validate_generated_project(dbt_dir)
            self.assertTrue(any("unique_key" in p and "fct_x" in p for p in problems))

    def test_merge_with_unique_key_and_on_schema_change_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = self._project(
                tmp,
                "{{ config(materialized='incremental', incremental_strategy='merge', "
                "unique_key='claim_id', on_schema_change='append_new_columns', "
                + _RETENTION
                + ") }}\nselect 1 as claim_id\n",
            )
            self.assertEqual(validate_generated_project(dbt_dir), [])

    def test_missing_on_schema_change_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = self._project(
                tmp,
                "{{ config(materialized='incremental', incremental_strategy='merge', "
                "unique_key='claim_id') }}\nselect 1 as claim_id\n",
            )
            problems = validate_generated_project(dbt_dir)
            self.assertTrue(any("on_schema_change" in p for p in problems))

    def test_microbatch_without_parent_event_time_is_refused_citing_full_scans(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = Path(tmp) / "dbt"
            (dbt_dir / "models" / "staging").mkdir(parents=True)
            (dbt_dir / "models" / "marts").mkdir(parents=True)
            (dbt_dir / "models" / "staging" / "stg_claims.sql").write_text(
                "{{ config(materialized='view') }}\nselect 1 as a\n", encoding="utf-8"
            )
            (dbt_dir / "models" / "marts" / "fct_x.sql").write_text(
                "{{ config(materialized='incremental', incremental_strategy='microbatch', "
                "unique_key='id', on_schema_change='append_new_columns', "
                "event_time='paid_at', lookback=2) }}\n"
                "-- WARNING: replace_where/microbatch inserts by POSITION, not by name.\n"
                "select id, paid_at from {{ ref('stg_claims') }}\n",
                encoding="utf-8",
            )
            problems = validate_generated_project(dbt_dir)
            self.assertTrue(
                any("stg_claims" in p and "PER BATCH" in p for p in problems), problems
            )

    def test_microbatch_select_star_and_missing_lookback_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = self._project(
                tmp,
                "{{ config(materialized='incremental', incremental_strategy='replace_where', "
                "unique_key='id', on_schema_change='append_new_columns', "
                "event_time='paid_at') }}\nselect * from x\n",
            )
            problems = validate_generated_project(dbt_dir)
            self.assertTrue(any("lookback" in p for p in problems))
            self.assertTrue(any("POSITION" in p and "select *" in p for p in problems))

    def test_table_without_cluster_by_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = self._project(
                tmp, "{{ config(materialized='table') }}\nselect 1 as a\n"
            )
            self.assertTrue(
                any("cluster_by" in p for p in validate_generated_project(dbt_dir))
            )

    def test_partition_by_below_one_terabyte_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = self._project(
                tmp,
                "{{ config(materialized='table', cluster_by=['a'], "
                "partition_by=['dt']) }}\nselect 1 as a\n",
            )
            self.assertTrue(
                any("partition_by" in p for p in validate_generated_project(dbt_dir))
            )

    def test_partition_by_with_a_declared_terabyte_size_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = self._project(
                tmp,
                "{{ config(materialized='table', cluster_by=['a'], "
                "partition_by=['dt'], partition_justified_bytes=2000000000000, "
                + _RETENTION
                + ") }}\nselect 1 as a\n",
            )
            self.assertEqual(validate_generated_project(dbt_dir), [])

    def test_identity_surrogate_key_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = self._project(
                tmp,
                "{{ config(materialized='table', cluster_by=['a']) }}\n"
                "select monotonically_increasing_id() as sk, 1 as a\n",
            )
            self.assertTrue(
                any("deterministic hash" in p for p in validate_generated_project(dbt_dir))
            )


class GhostTableReconcileTests(unittest.TestCase):
    def test_reports_orphans_and_never_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dbt_dir = _generate(root)
            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            report_path = reconcile_ghost_tables(
                dbt_dir,
                ["fct_kpi_001", "fct_kpi_999_renamed_away", "stg_transactions"],
                layout=layout,
                schema="gold",
            )
            self.assertTrue(report_path.endswith("dbt_reconcile/current.md"))
            report = Path(report_path).read_text(encoding="utf-8")
            self.assertIn("fct_kpi_999_renamed_away", report)
            self.assertNotIn("fct_kpi_001", report.split("## Orphaned relations")[-1])
            self.assertNotIn("DROP", report.upper())

    def test_clean_project_reports_no_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dbt_dir = _generate(root)
            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            report = Path(
                reconcile_ghost_tables(dbt_dir, ["fct_kpi_001"], layout=layout)
            ).read_text(encoding="utf-8")
            self.assertIn("No orphaned relations found", report)


def _manifest_dbt_dir(tmp: Path, models: dict[str, dict]) -> Path:
    """A bare dbt project dir carrying only `target/manifest.json` -- the
    shape reconcile reads from without a real `dbt build`."""
    dbt_dir = tmp / "dbt"
    (dbt_dir / "target").mkdir(parents=True)
    (dbt_dir / "models").mkdir(parents=True)
    nodes = {
        f"model.demo.{unique_id}": {"resource_type": "model", **fields}
        for unique_id, fields in models.items()
    }
    (dbt_dir / "target" / "manifest.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )
    return dbt_dir


class GhostTableReconcileRelationNameTests(unittest.TestCase):
    """Two models can share a bare alias across different schemas -- diffing
    on the alias alone collides them. dbt's manifest carries the fully
    qualified `relation_name` for exactly this reason."""

    def test_reports_fully_qualified_orphan_and_never_a_bare_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _manifest_dbt_dir(
                Path(tmp),
                {
                    "fct_x": {
                        "alias": "fct_x",
                        "name": "fct_x",
                        "relation_name": "`cat`.`gold`.`fct_x`",
                    }
                },
            )
            layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
            report_path = reconcile_ghost_tables(
                dbt_dir, ["cat.gold.fct_x", "cat.gold.fct_orphan"], layout=layout
            )
            report = Path(report_path).read_text(encoding="utf-8")
            orphan_section = report.split("## Orphaned relations")[-1]
            self.assertIn("cat.gold.fct_orphan", orphan_section)
            self.assertNotIn("cat.gold.fct_x", orphan_section)
            self.assertNotIn("DROP", report.upper())

    def test_bare_alias_collision_across_schemas_is_not_a_false_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The project's only model is cat.gold.fct_x. A DIFFERENT schema
            # (cat.silver) also has a table aliased fct_x -- a bare-alias diff
            # would wrongly treat it as "still in the project".
            dbt_dir = _manifest_dbt_dir(
                Path(tmp),
                {
                    "fct_x": {
                        "alias": "fct_x",
                        "name": "fct_x",
                        "relation_name": "`cat`.`gold`.`fct_x`",
                    }
                },
            )
            layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
            report_path = reconcile_ghost_tables(
                dbt_dir, ["cat.silver.fct_x"], layout=layout
            )
            report = Path(report_path).read_text(encoding="utf-8")
            orphan_section = report.split("## Orphaned relations")[-1]
            self.assertIn("cat.silver.fct_x", orphan_section)
            self.assertNotIn("DROP", report.upper())

    def test_pre_1_0_manifest_without_relation_name_falls_back_to_alias_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbt_dir = _manifest_dbt_dir(
                Path(tmp), {"fct_x": {"alias": "fct_x", "name": "fct_x"}}
            )
            layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
            report = Path(
                reconcile_ghost_tables(dbt_dir, ["fct_x", "fct_orphan"], layout=layout)
            ).read_text(encoding="utf-8")
            orphan_section = report.split("## Orphaned relations")[-1]
            self.assertIn("fct_orphan", orphan_section)
            self.assertNotIn("fct_x", orphan_section.replace("fct_orphan", ""))
            self.assertNotIn("DROP", report.upper())


if __name__ == "__main__":
    unittest.main()


class DbtParseGateTests(unittest.TestCase):
    """`dbt parse` resolves every ref/source/Jinja/config in one offline pass.

    It lives in `verify-dbt-project`, NOT in generation: parse has to render
    profiles.yml, which resolves credentials via env_var(), and generation must
    work on a machine that never connects to a warehouse.
    """

    def test_missing_dbt_binary_is_a_reported_skip_not_a_failure(self):
        with mock.patch.object(dbt_project_generator.shutil, "which", return_value=None):
            ok, detail = dbt_project_generator.run_dbt_parse(Path("proj"))
        self.assertTrue(ok)
        self.assertTrue(detail.startswith("[~]"), detail)
        self.assertIn("skipped", detail)

    def test_parse_failure_surfaces_dbt_own_output(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return types.SimpleNamespace(
                returncode=2, stdout="", stderr="Compilation Error in model fct_x"
            )

        with mock.patch.object(dbt_project_generator.shutil, "which", return_value="dbt"):
            ok, detail = dbt_project_generator.run_dbt_parse(Path("proj"), runner=fake_run)
        self.assertFalse(ok)
        self.assertIn("Compilation Error in model fct_x", detail)
        self.assertEqual(calls[0][1], "parse")
        self.assertIn("--project-dir", calls[0])
        self.assertIn("--profiles-dir", calls[0])

    def test_clean_parse_passes(self):
        def fake_run(cmd, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with mock.patch.object(dbt_project_generator.shutil, "which", return_value="dbt"):
            ok, detail = dbt_project_generator.run_dbt_parse(Path("proj"), runner=fake_run)
        self.assertTrue(ok)
        self.assertTrue(detail.startswith("[ok]"), detail)

    def test_generation_does_not_shell_out_to_dbt(self):
        """Generation must not require a dbt binary or credentials."""
        source = Path("core/onboarding/kpi/dbt_project_generator.py").read_text(
            encoding="utf-8"
        )
        generate_body = source.split("def generate(", 1)[1].split("\n    def ", 1)[0]
        self.assertNotIn("run_dbt_parse(", generate_body)


class EmptySelectionTests(unittest.TestCase):
    def test_emitted_project_promotes_empty_selection_to_an_error(self):
        """`dbt ls --select nonexistent` exits 0; a stale selector must not look green."""
        text = Path("core/onboarding/kpi/dbt_project_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("NoNodesForSelectionCriteria", text)
        self.assertIn("warn_error_options", text)

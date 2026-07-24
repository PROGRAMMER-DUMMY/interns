"""core.onboarding.kpi.data_quality_panel: data-quality rules authored the
same human-in-the-loop way KPI features already are -- evidence-backed
options, Human-Gate Provenance, generated dbt tests, never a bespoke
freehand questionnaire.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.data_source_panel import DataSourceAnswerRecorder
from core.onboarding.kpi.data_quality_panel import (
    DataQualityAnswerRecorder,
    DataQualityPanelBuilder,
    _looks_categorical,
)
from core.onboarding.kpi.dbt_project_generator import DbtProjectGenerator
from core.onboarding.kpi.feature_resolver import KPIFeatureResolver
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.storage.workspace_layout import WorkspaceLayout


def _create_workspace(root: Path) -> Path:
    workspace = root / "workspaces" / "demo"
    (workspace / "datasets").mkdir(parents=True)
    (workspace / "docs").mkdir(parents=True)
    (workspace / "datasets" / "transactions.csv").write_text(
        "ClaimID,PaidAmount,LineOfBusiness\n"
        "C1,10.50,Commercial\n"
        "C2,20.25,Medicare\n",
        encoding="utf-8",
    )
    (workspace / "docs" / "kpi_registry.csv").write_text(
        "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
        "What is paid amount by line of business?,Baseline KPI,LineOfBusiness,sum(PaidAmount),\n",
        encoding="utf-8",
    )
    return workspace


class LooksCategoricalTests(unittest.TestCase):
    def test_string_dtypes_are_categorical(self):
        self.assertTrue(_looks_categorical("String"))
        self.assertTrue(_looks_categorical("string"))
        self.assertTrue(_looks_categorical("varchar(50)"))

    def test_numeric_temporal_dtypes_are_not_categorical(self):
        for dtype in ("Float64", "double", "decimal(10,2)", "bigint", "date", "boolean", "Int64"):
            self.assertFalse(_looks_categorical(dtype), dtype)

    def test_empty_dtype_is_not_categorical(self):
        self.assertFalse(_looks_categorical(""))


class DataQualityPanelBuilderTests(unittest.TestCase):
    def test_no_kpi_ready_yet_reports_no_pending_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            WorkspaceLayout(project_root=workspace).ensure_runtime_dirs()
            result = DataQualityPanelBuilder(root, "workspaces/demo").prepare()
            self.assertEqual(result.status, "no_pending_checks")

    def test_categorical_column_surfaces_accepted_values_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()

            result = DataQualityPanelBuilder(root, "workspaces/demo").prepare()
            self.assertEqual(result.status, "needs_user_answer")
            panel = json.loads((root / result.current_json_path).read_text(encoding="utf-8"))
            # Numeric PaidAmount must NOT be offered as a categorical
            # accepted-values candidate just because a 2-row fixture makes
            # its sample look small.
            self.assertEqual(panel["column"], "LineOfBusiness")
            option_ids = {o["option_id"] for o in panel["options"]}
            self.assertEqual(option_ids, {"option_a", "option_b", "skip"})
            self.assertEqual(panel["options"][0]["check_type"], "accepted_values")
            self.assertIn("Commercial", panel["options"][0]["check_config"]["values"])
            self.assertIn("Medicare", panel["options"][0]["check_config"]["values"])

    def test_answering_moves_to_no_pending_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            DataQualityPanelBuilder(root, "workspaces/demo").prepare()

            outcome = DataQualityAnswerRecorder(root, "workspaces/demo").apply(
                "option_a", confirmed_by="shubham"
            )
            self.assertEqual(outcome["check_type"], "accepted_values")
            self.assertEqual(outcome["source"], "human")
            self.assertEqual(outcome["next_panel"]["status"], "no_pending_checks")

            decisions = json.loads(
                (root / outcome["decisions_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(len(decisions["decisions"]), 1)
            self.assertEqual(decisions["decisions"][0]["severity"], "error")  # LineOfBusiness feeds a KPI cut

    def test_empty_confirmed_by_records_agent_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            DataQualityPanelBuilder(root, "workspaces/demo").prepare()
            outcome = DataQualityAnswerRecorder(root, "workspaces/demo").apply("skip")
            self.assertEqual(outcome["source"], "agent")
            self.assertEqual(outcome["check_type"], "")  # skip records no check

    def test_invalid_answer_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            DataQualityPanelBuilder(root, "workspaces/demo").prepare()
            with self.assertRaises(ValueError):
                DataQualityAnswerRecorder(root, "workspaces/demo").apply("option_z")

    def test_no_panel_yet_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            WorkspaceLayout(project_root=workspace).ensure_runtime_dirs()
            with self.assertRaises(FileNotFoundError):
                DataQualityAnswerRecorder(root, "workspaces/demo").apply("option_a")


class DataQualityDbtIntegrationTests(unittest.TestCase):
    def test_confirmed_decision_emits_dbt_schema_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            DataQualityPanelBuilder(root, "workspaces/demo").prepare()
            DataQualityAnswerRecorder(root, "workspaces/demo").apply(
                "option_a", confirmed_by="shubham"
            )

            result = DbtProjectGenerator(
                root, "workspaces/demo", catalog="main", schema="rcm"
            ).generate()
            dbt_dir = root / result.dbt_project_dir
            dq_yml = (dbt_dir / "models" / "staging" / "_data_quality.yml").read_text(
                encoding="utf-8"
            )
            self.assertIn("name: stg_transactions", dq_yml)
            self.assertIn("name: LineOfBusiness", dq_yml)
            self.assertIn("accepted_values:", dq_yml)
            self.assertIn("Commercial", dq_yml)
            self.assertIn("severity: error", dq_yml)

    def test_skip_answer_emits_no_test_and_no_schema_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            DataQualityPanelBuilder(root, "workspaces/demo").prepare()
            DataQualityAnswerRecorder(root, "workspaces/demo").apply("skip")

            result = DbtProjectGenerator(
                root, "workspaces/demo", catalog="main", schema="rcm"
            ).generate()
            dbt_dir = root / result.dbt_project_dir
            self.assertFalse((dbt_dir / "models" / "staging" / "_data_quality.yml").exists())

    def test_no_decisions_at_all_is_byte_identical_to_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_workspace(root)
            WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()
            KPIFeatureResolver(root, "workspaces/demo", domain="healthcare").run()
            # No data_quality_panel ever run at all.
            result = DbtProjectGenerator(
                root, "workspaces/demo", catalog="main", schema="rcm"
            ).generate()
            dbt_dir = root / result.dbt_project_dir
            self.assertFalse((dbt_dir / "models" / "staging" / "_data_quality.yml").exists())


if __name__ == "__main__":
    unittest.main()

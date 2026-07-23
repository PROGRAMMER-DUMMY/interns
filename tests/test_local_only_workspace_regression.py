"""Golden-fixture regression guard for the Databricks-first-class onboarding
work (Phases A/B/C).

A workspace that declares no ``databricks_source`` at all must behave
IDENTICALLY before and after every phase of that work lands -- this is the
literal proof that "never break the existing local-only default" held, not
just an assertion of intent. Re-run this file after each phase; it must stay
green throughout with zero edits to its own assertions.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.onboarding import WorkspaceOnboarder


class LocalOnlyWorkspaceRegressionTests(unittest.TestCase):
    def _fixture_workspace(self, root: Path) -> Path:
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

    def test_no_databricks_source_declared_at_all(self):
        # No workspace_settings.json written -- the common case, every
        # workspace onboarded before this work existed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._fixture_workspace(root)

            result = WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            self.assertEqual(result.inputs.data_files, ["workspaces/demo/datasets/transactions.csv"])
            self.assertEqual(result.inputs.databricks_tables, [])
            self.assertEqual(result.kpi_count, 1)
            self.assertEqual(result.profile_count, 1)
            self.assertEqual(result.warnings, [])

            profile_index = json.loads(
                (workspace / "interns" / "generated" / "profiles" / "profile_index.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(len(profile_index["profiles"]), 1)
            profile = profile_index["profiles"][0]
            self.assertEqual(profile["format"], "csv")
            self.assertIn("transactions.csv", profile["path"])

    def test_workspace_settings_present_but_no_databricks_source_key(self):
        # workspace_settings.json exists (e.g. for an unrelated setting like
        # dataset_allowlist) but never mentions databricks_source at all.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._fixture_workspace(root)
            (workspace / "workspace_settings.json").write_text(
                json.dumps({"dataset_allowlist": ["datasets"]}), encoding="utf-8"
            )

            result = WorkspaceOnboarder(root, "workspaces/demo", sample_rows=10).run()

            self.assertEqual(result.inputs.data_files, ["workspaces/demo/datasets/transactions.csv"])
            self.assertEqual(result.inputs.databricks_tables, [])
            self.assertEqual(result.kpi_count, 1)
            self.assertEqual(result.profile_count, 1)


if __name__ == "__main__":
    unittest.main()

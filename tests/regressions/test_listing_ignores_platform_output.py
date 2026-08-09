"""F18: the workspace file classifier must not read the platform's own output
back in as source data.

`generate-ingestion`, `generate-dbt-project` and `sync-workspace-code` write
`ingestion/`, `dbt/` and `context/` inside the workspace, and the Databricks CLI
leaves `.databricks/sync-snapshots/`. Those are emissions, not inputs. Counting
their `.json` as `dataset_evidence` made WS-BUG-001 ("listing finds inputs but
onboarding artifacts are empty") fire at CRITICAL on every cloud-native
workspace once ingestion had been generated -- which hard-blocks
`prepare-kpi-blocker-panel`, and with it the whole KPI path.

`interns/` was already excluded upstream; these directories postdate that rule.
"""
from __future__ import annotations

import unittest

from core.provisioning.sync_code import CODE_DIRS
from tools.list_workspace_files import (
    PLATFORM_OUTPUT_DIRS,
    _file_roles_and_reasons,
)


class PlatformOutputIsNotDatasetEvidenceTests(unittest.TestCase):
    def test_generated_ingestion_manifest_is_not_dataset_evidence(self):
        roles, _ = _file_roles_and_reasons(
            "workspaces/demo/ingestion/jobs_manifest.json"
        )
        self.assertNotIn("dataset_evidence", roles)

    def test_published_context_contract_is_not_dataset_evidence(self):
        roles, _ = _file_roles_and_reasons(
            "workspaces/demo/context/domain_model.json"
        )
        self.assertNotIn("dataset_evidence", roles)

    def test_databricks_sync_snapshot_is_not_dataset_evidence(self):
        roles, _ = _file_roles_and_reasons(
            "workspaces/demo/ingestion/.databricks/sync-snapshots/abc.json"
        )
        self.assertNotIn("dataset_evidence", roles)

    def test_generated_dbt_artifact_is_not_dataset_evidence(self):
        roles, _ = _file_roles_and_reasons(
            "workspaces/demo/dbt/target/manifest.json"
        )
        self.assertNotIn("dataset_evidence", roles)


class RealInputsStillCountTests(unittest.TestCase):
    """The exclusion must be narrow: real source data still has to register, or
    the detector stops catching the empty-onboarding case it exists for."""

    def test_a_real_dataset_csv_is_still_dataset_evidence(self):
        roles, _ = _file_roles_and_reasons("workspaces/demo/datasets/claims.csv")
        self.assertIn("dataset_evidence", roles)

    def test_a_root_level_parquet_is_still_dataset_evidence(self):
        roles, _ = _file_roles_and_reasons("workspaces/demo/encounters.parquet")
        self.assertIn("dataset_evidence", roles)

    def test_a_dataset_named_like_a_platform_dir_still_counts(self):
        # 'context' as a FILE stem, not a directory segment, is user data.
        roles, _ = _file_roles_and_reasons("workspaces/demo/datasets/context.csv")
        self.assertIn("dataset_evidence", roles)


class ExclusionTracksTheGeneratorTests(unittest.TestCase):
    def test_every_synced_code_dir_is_excluded(self):
        # sync_code.CODE_DIRS is the authority on what the platform writes into
        # a workspace; this listing is deliberately a light copy (the selection
        # path must stay import-cheap), so pin them together against drift.
        for name in CODE_DIRS:
            self.assertIn(
                name, PLATFORM_OUTPUT_DIRS,
                f"sync-workspace-code ships {name!r} but the listing still reads it back as input",
            )


if __name__ == "__main__":
    unittest.main()

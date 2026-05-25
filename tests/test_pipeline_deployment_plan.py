from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.onboarding.pipeline_deployment_plan import (
    REMOTE_APPROVAL_ENV,
    PipelineDeploymentPlanner,
)


class PipelineDeploymentPlanTests(unittest.TestCase):
    def _workspace_with_contracts(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        contracts = workspace / "interns" / "generated" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / "catalog_contract.json").write_text(
            json.dumps(
                {
                    "artifact_type": "catalog_contract.json",
                    "workspace": "workspaces/demo",
                    "objects": [{"logical_name": "raw.transactions"}],
                    "policy": {"remote_mutation_requires_explicit_approval": True},
                }
            ),
            encoding="utf-8",
        )
        (contracts / "pipeline_plan.json").write_text(
            json.dumps(
                {
                    "artifact_type": "pipeline_plan.json",
                    "workspace": "workspaces/demo",
                    "status": "ready_for_generation",
                    "layers": [
                        {
                            "layer": "bronze",
                            "operation": "define_bronze_contracts",
                            "objects": [{"target_object": "bronze.transactions"}],
                        }
                    ],
                    "policy": {"remote_writes_require_explicit_approval": True},
                }
            ),
            encoding="utf-8",
        )
        return workspace

    def test_dry_run_success_writes_deployment_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace_with_contracts(root)

            result = PipelineDeploymentPlanner(
                root,
                "workspaces/demo",
                target="warehouse",
            ).build()

            self.assertEqual(result.mode, "dry-run")
            self.assertEqual(result.status, "dry_run_ready")
            plan = json.loads((root / result.json_path).read_text(encoding="utf-8"))
            self.assertTrue(plan["remote_writes_require_explicit_approval"])
            self.assertTrue(plan["execution_policy"]["no_actual_remote_mutation"])
            self.assertFalse(plan["execution_policy"]["remote_mutation_performed"])
            self.assertEqual(plan["deployment_actions"][0]["mutation"], "none")
            self.assertTrue((root / result.markdown_path).exists())

    def test_unapproved_remote_apply_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._workspace_with_contracts(root)
            output = workspace / "interns" / "generated" / "contracts" / "pipeline_deployment_plan.json"

            with patch.dict("os.environ", {REMOTE_APPROVAL_ENV: ""}, clear=False):
                with self.assertRaises(PermissionError) as raised:
                    PipelineDeploymentPlanner(
                        root,
                        "workspaces/demo",
                        target="external",
                        mode="apply",
                    ).build()

            self.assertIn(REMOTE_APPROVAL_ENV, str(raised.exception))
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

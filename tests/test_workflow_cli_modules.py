from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from io import StringIO

from core.onboarding import (
    data_model_generation_cli,
    data_model_generation_workflow,
    kpi_blocker_cli,
    kpi_blocker_workflow,
    kpi_generation_cli,
    kpi_generation_workflow,
)


class WorkflowCLIModuleTests(unittest.TestCase):
    def test_kpi_generation_workflow_cli_wrappers_delegate_to_cli_module(self):
        self.assertIsNot(kpi_generation_workflow.prepare_main, kpi_generation_cli.prepare_main)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            kpi_generation_workflow.prepare_main([])

    def test_data_model_workflow_cli_wrappers_delegate_to_cli_module(self):
        self.assertIsNot(data_model_generation_workflow.prepare_main, data_model_generation_cli.prepare_main)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            data_model_generation_workflow.prepare_main([])

    def test_kpi_blocker_workflow_cli_wrappers_delegate_to_cli_module(self):
        self.assertIsNot(kpi_blocker_workflow.prepare_main, kpi_blocker_cli.prepare_main)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            kpi_blocker_workflow.prepare_main([])


if __name__ == "__main__":
    unittest.main()

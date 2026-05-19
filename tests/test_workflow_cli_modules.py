from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from io import StringIO

from core.onboarding import (
    agent_benchmark,
    data_model_generation_cli,
    data_model_generation_workflow,
    kpi_blocker_cli,
    kpi_blocker_workflow,
    kpi_generation_cli,
    kpi_generation_workflow,
    wiki_memory,
    workspace_workflow,
)


class WorkflowCLIModuleTests(unittest.TestCase):
    def test_kpi_generation_workflow_cli_wrappers_delegate_to_cli_module(self):
        self.assertIsNot(kpi_generation_workflow.prepare_main, kpi_generation_cli.prepare_main)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            kpi_generation_workflow.prepare_main([])

    def test_data_model_workflow_cli_wrappers_delegate_to_cli_module(self):
        self.assertIsNot(data_model_generation_workflow.prepare_main, data_model_generation_cli.prepare_main)
        self.assertIsNot(
            data_model_generation_workflow.prepare_blocker_main,
            data_model_generation_cli.prepare_blocker_main,
        )
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            data_model_generation_workflow.prepare_main([])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            data_model_generation_workflow.prepare_blocker_main([])

    def test_kpi_blocker_workflow_cli_wrappers_delegate_to_cli_module(self):
        self.assertIsNot(kpi_blocker_workflow.prepare_main, kpi_blocker_cli.prepare_main)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            kpi_blocker_workflow.prepare_main([])

    def test_workspace_workflow_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            workspace_workflow.prepare_main([])

    def test_wiki_memory_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            wiki_memory.prepare_main([])

    def test_agent_benchmark_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            agent_benchmark.prepare_main([])


if __name__ == "__main__":
    unittest.main()

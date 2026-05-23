from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from io import StringIO

from core.onboarding.benchmark import agent_benchmark
from core.onboarding.data_model import generation_cli as data_model_generation_cli
from core.onboarding.data_model import generation_workflow as data_model_generation_workflow
from core.onboarding import evidence_graph
from core.onboarding.harness import ai_app_harness, ai_cli_harness, trajectory_recorder, workflow_guard_harness
from core.onboarding.kpi import blocker_cli as kpi_blocker_cli
from core.onboarding.kpi import blocker_workflow as kpi_blocker_workflow
from core.onboarding.kpi import generation_cli as kpi_generation_cli
from core.onboarding.kpi import generation_workflow as kpi_generation_workflow
from core.onboarding.kpi import proof_packet
from core.onboarding.memory import wiki_memory
from core.onboarding.workspace import flow as workspace_flow
from core.onboarding.workspace import workflow as workspace_workflow


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

    def test_workspace_flow_cli_requires_subcommand(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            workspace_flow.main([])

    def test_wiki_memory_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            wiki_memory.prepare_main([])

    def test_agent_benchmark_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            agent_benchmark.prepare_main([])

    def test_kpi_proof_packet_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            proof_packet.main([])

    def test_ai_app_harness_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            ai_app_harness.main([])

    def test_ai_cli_harness_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            ai_cli_harness.main([])

    def test_workflow_guard_harness_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            workflow_guard_harness.main([])

    def test_trajectory_recorder_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            trajectory_recorder.main([])

    def test_evidence_graph_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            evidence_graph.main([])

    def test_evidence_graph_query_cli_requires_workspace(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            evidence_graph.query_main([])


if __name__ == "__main__":
    unittest.main()

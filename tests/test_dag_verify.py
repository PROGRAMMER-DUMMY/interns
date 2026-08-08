"""Offline orchestration-graph verification.

The DAG modules here are static and rendered from one stage graph, so the useful
gate is not "does the DAG file parse" -- it is "does every command the graph
shells out to still exist, and would a scheduler accept the topology". A renamed
CLI is invisible until a scheduled task fails in production, because the command
name lives in a string.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from core.dev.instruction_drift import registered_commands
from core.orchestration import dag_verify
from core.paths import PROJECT_ROOT


@dataclass(frozen=True)
class _Stage:
    key: str
    upstream: tuple[str, ...] = ()


class EmittedCommandTests(unittest.TestCase):
    def test_every_emitted_command_is_registered(self):
        """The check this module exists for, run against the real graph."""
        result = dag_verify.verify_dags()
        self.assertEqual(
            result.unknown_commands,
            [],
            "an orchestration stage shells out to a command that is not in "
            "pyproject.toml [project.scripts]; a scheduled task will fail at "
            "runtime. Register it, or update the stage to the current name.",
        )

    def test_interpreter_passthrough_is_not_treated_as_a_script(self):
        """`uv run python -m ...` is the interpreter, not a project script."""
        self.assertNotIn("python", dag_verify.emitted_commands())

    def test_commands_are_discovered_from_the_real_modules(self):
        emitted = dag_verify.emitted_commands()
        self.assertIn("onboard-workspace", emitted)
        self.assertIn(
            "pipeline_stages.py",
            emitted["onboard-workspace"],
            "onboard-workspace should be traced back to the module that emits it",
        )

    def test_emitted_commands_resolve_against_pyproject(self):
        registered = registered_commands(PROJECT_ROOT / "pyproject.toml")
        for command in dag_verify.emitted_commands():
            self.assertIn(command, registered, f"{command} is not a registered script")


class TopologyTests(unittest.TestCase):
    def test_real_graph_topology_is_clean(self):
        self.assertEqual(dag_verify.topology_errors(), [])

    def test_duplicate_stage_id_is_reported(self):
        errors = dag_verify.topology_errors([_Stage("a"), _Stage("a")])
        self.assertTrue(any("duplicate stage id: a" in e for e in errors), errors)

    def test_dangling_dependency_is_reported(self):
        errors = dag_verify.topology_errors([_Stage("a", ("nope",))])
        self.assertTrue(any("not a stage" in e for e in errors), errors)

    def test_cycle_is_reported(self):
        errors = dag_verify.topology_errors([_Stage("a", ("b",)), _Stage("b", ("a",))])
        self.assertTrue(any("cycle" in e for e in errors), errors)

    def test_a_clean_chain_is_not_a_cycle(self):
        errors = dag_verify.topology_errors(
            [_Stage("a"), _Stage("b", ("a",)), _Stage("c", ("a", "b"))]
        )
        self.assertEqual(errors, [])


class VerdictTests(unittest.TestCase):
    def test_real_graph_verifies(self):
        result = dag_verify.verify_dags()
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.status, "verified")
        self.assertGreater(result.stage_count, 0)
        self.assertGreater(result.command_count, 0)

    def test_deprecated_command_is_a_warning_not_a_failure(self):
        """A deprecated CLI still resolves, so it must not fail the gate --
        but leaving a scheduled task pinned to one is worth naming."""
        result = dag_verify.verify_dags()
        self.assertTrue(result.ok)
        self.assertIsInstance(result.deprecated_commands, list)

    def test_exit_code_follows_the_verdict(self):
        self.assertEqual(dag_verify.main([]), 0)


if __name__ == "__main__":
    unittest.main()

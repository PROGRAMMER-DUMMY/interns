"""Regressions for Q7 (lingering-issues plan): dead-code cleanup + duplicate
path-helper consolidation. See ~/.claude/plans/dynamic-cooking-firefly.md Q7.

Confirmed-dead code removed: medallion/prompt_strategies.py, agents.StubLLMEngine,
workspace_deployer.py's post_api. Confirmed test-covered/actively-called code
was deliberately NOT deleted (budget.py has real tests + a tracked future-phase
role; tier_router.py is actively called by design.py; contracts/versioning.migrate
has 5 passing tests) -- see the plan file for the full reasoning.

core.paths.rel_to consolidates the ~_rel(path, root) duplication in 4 modules
(onboarding.py, contracts.py, catalog.py, project_harness.py); the two
medallion_design_naming baseline failures the plan named were investigated and
found already fixed (stale plan item, no change needed).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.paths import rel_to


class RelToConsolidationTests(unittest.TestCase):
    def test_posix_normalized_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "a" / "b.txt"
            nested.parent.mkdir(parents=True)
            nested.write_text("x", encoding="utf-8")
            self.assertEqual(rel_to(nested, root), "a/b.txt")

    def test_fallback_is_also_posix_normalized(self):
        # A path outside root: always posix, even on the not-relative fallback
        # (the prior copies disagreed here -- some returned OS-native str()).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            outside = Path(tmp) / "elsewhere" / "c.txt"
            outside.parent.mkdir()
            outside.write_text("x", encoding="utf-8")
            result = rel_to(outside, root)
            self.assertNotIn("\\", result)

    def test_four_consolidated_modules_import_rel_to(self):
        import ast

        modules = [
            "core/onboarding/workspace/onboarding.py",
            "core/onboarding/relationships/contracts.py",
            "core/onboarding/sources/catalog.py",
            "core/onboarding/harness/project_harness.py",
        ]
        repo_root = Path(__file__).resolve().parents[2]
        for rel_path in modules:
            source = (repo_root / rel_path).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imports_rel_to = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "core.paths"
                and any(alias.name == "rel_to" for alias in node.names)
                for node in ast.walk(tree)
            )
            self.assertTrue(imports_rel_to, f"{rel_path} does not import core.paths.rel_to")
            # The local duplicate definition must be gone (only the import remains).
            local_def = any(
                isinstance(node, ast.FunctionDef) and node.name == "_rel"
                for node in ast.walk(tree)
            )
            self.assertFalse(local_def, f"{rel_path} still has a local _rel() definition")


class DeadCodeRemovedTests(unittest.TestCase):
    def test_prompt_strategies_module_is_gone(self):
        with self.assertRaises(ModuleNotFoundError):
            import core.medallion.prompt_strategies  # noqa: F401

    def test_stub_llm_engine_removed(self):
        import core.agents.llm_engine as llm_engine

        self.assertFalse(hasattr(llm_engine, "StubLLMEngine"))
        import core.agents as agents

        self.assertFalse(hasattr(agents, "StubLLMEngine"))

    def test_post_api_removed_from_workspace_deployer(self):
        from core.onboarding.databricks.workspace_deployer import DatabricksWorkspaceApi

        self.assertFalse(hasattr(DatabricksWorkspaceApi, "post_api"))

    def test_deliberately_kept_tested_code_still_present(self):
        # budget.py, tier_router.py, and contracts.versioning.migrate are NOT
        # dead by the "no test references it" bar -- confirm they were not
        # accidentally swept up in the cleanup.
        from core.medallion.budget import BudgetTracker  # noqa: F401
        from core.medallion.tier_router import assign_tiers, pick_model, rank_models  # noqa: F401
        from core.contracts.versioning import migrate, register_migration  # noqa: F401


if __name__ == "__main__":
    unittest.main()

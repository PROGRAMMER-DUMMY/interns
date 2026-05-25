import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.sources.external_intake_cli import apply_main, prepare_main
from core.onboarding.sources.external_intake_workflow import ExternalSourceIntakeWorkflow


class ExternalSourceIntakeWorkflowTests(unittest.TestCase):
    def _external_root(self, root: Path) -> Path:
        external = root / "external"
        group = external / "raw" / "CMS Claims"
        group.mkdir(parents=True)
        (group / "claims.csv").write_text("id,amount\n1,10\n", encoding="utf-8")
        (group / "Claims Data Dictionary.pdf").write_text("fake pdf", encoding="utf-8")
        return external

    def test_prepare_asks_route_and_apply_creates_workspace_runs_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = self._external_root(root)

            prepared = ExternalSourceIntakeWorkflow(
                root,
                external_root=external,
                proposed_workspace="workspaces/cms",
            ).prepare()
            self.assertEqual(prepared.stage, "route_selection")
            panel = json.loads((root / prepared.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(panel["recommended_option_id"], "option_a")

            applied = ExternalSourceIntakeWorkflow(
                root,
                external_root=external,
                proposed_workspace="workspaces/cms",
            ).apply_answer(answer="option_a", save_as_default=True)

            self.assertEqual(applied.workspace, "workspaces/cms")
            self.assertEqual(applied.stage, "outcome_selection")
            self.assertTrue(
                (
                    root
                    / "workspaces/cms/interns/generated/requirements/external_source_discovery.json"
                ).exists()
            )
            self.assertTrue(
                (root / "state/team_memory/external_source_intake_preferences.json").exists()
            )
            memory = json.loads(
                (root / "state/team_memory/external_source_intake_preferences.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(memory["preferences"]["default_route"], "create_new")

    def test_existing_proposed_workspace_becomes_attach_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = self._external_root(root)
            existing = root / "workspaces" / "Healthcare-RCM-Data-Platform"
            (existing / "docs").mkdir(parents=True)
            (existing / "docs" / "Sample KPI.xlsx").write_text("placeholder", encoding="utf-8")

            prepared = ExternalSourceIntakeWorkflow(
                root,
                external_root=external,
                proposed_workspace="workspaces/Healthcare-RCM-Data-Platform",
            ).prepare()

            panel = json.loads((root / prepared.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(prepared.workspace, "workspaces/external")
            self.assertEqual(panel["options"][0]["label"], "Create `workspaces/external`")
            self.assertEqual(
                panel["options"][1]["label"],
                "Attach to `workspaces/Healthcare-RCM-Data-Platform`",
            )

    def test_empty_proposed_workspace_is_honored_as_external_data_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = self._external_root(root)
            existing = root / "workspaces" / "CMS_Medical"
            existing.mkdir(parents=True)

            prepared = ExternalSourceIntakeWorkflow(
                root,
                external_root=external,
                proposed_workspace="workspaces/CMS_Medical",
            ).prepare()

            panel = json.loads((root / prepared.current_json_path).read_text(encoding="utf-8"))
            self.assertEqual(prepared.workspace, "workspaces/CMS_Medical")
            self.assertEqual(panel["options"][0]["label"], "Create `workspaces/CMS_Medical`")
            self.assertEqual(panel["options"][1]["label"], "Attach to existing workspace")

    def test_apply_outcome_then_group_policy_writes_workspace_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = self._external_root(root)
            workflow = ExternalSourceIntakeWorkflow(root, external_root=external, proposed_workspace="workspaces/cms")
            workflow.prepare()
            workflow.apply_answer(answer="option_a")

            outcome = workflow.apply_answer(answer="option_c")
            self.assertEqual(outcome.stage, "source_group_selection")
            groups = workflow.apply_answer(answer="option_a")
            self.assertEqual(groups.stage, "approval_gate")
            memory = json.loads(
                (
                    root
                    / "workspaces/cms/interns/generated/memory/external_source_intake_memory.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(memory["preferences"]["intended_outcome"], "medallion_etl")
            self.assertIn("raw/CMS Claims", memory["selected_groups"])

    def test_saved_default_change_requires_reason_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = self._external_root(root)
            memory_path = root / "state" / "team_memory" / "external_source_intake_preferences.json"
            memory_path.parent.mkdir(parents=True)
            memory_path.write_text(
                json.dumps({"preferences": {"default_route": "create_new", "default_workspace": "workspaces/cms"}}),
                encoding="utf-8",
            )
            existing = root / "workspaces" / "existing"
            existing.mkdir(parents=True)

            workflow = ExternalSourceIntakeWorkflow(root, external_root=external, proposed_workspace="workspaces/cms")
            workflow.prepare()
            changed = workflow.apply_answer(answer="option_b", existing_workspace="workspaces/existing")

            self.assertEqual(changed.stage, "route_change_reason")
            continued = workflow.apply_answer(
                answer="option_a",
                existing_workspace="workspaces/existing",
                change_reason="belongs to existing project",
            )
            self.assertEqual(continued.workspace, "workspaces/existing")
            self.assertEqual(continued.stage, "outcome_selection")

    def test_cli_prepare_and_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = self._external_root(root)
            self.assertEqual(
                prepare_main(
                    [
                        "--repo-root",
                        str(root),
                        "--external-root",
                        str(external),
                        "--proposed-workspace",
                        "workspaces/cms",
                    ]
                ),
                0,
            )
            self.assertEqual(
                apply_main(
                    [
                        "--repo-root",
                        str(root),
                        "--external-root",
                        str(external),
                        "--proposed-workspace",
                        "workspaces/cms",
                        "--answer",
                        "option_a",
                    ]
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()

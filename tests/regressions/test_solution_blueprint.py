"""Regression: propose the whole plan, and get it approved, before building it.

Origin (2026-07-27): asked to build a pipeline from `s3://amzn-workspace-rcm/`
onto Databricks, the platform asked what to name a folder. It never listed the
bucket, never said which files would become tables and which would not, never
stated where dbt would run or what modelling technique it would use, and never
showed a plan to approve.

The blueprint runs first and fixes the ordering. These tests pin the properties
that make it trustworthy rather than decorative:

  * nothing is created before approval, and the emitted commands stay emitted;
  * the table-vs-volume split is real (they are different Unity Catalog
    securables -- volume files cannot be registered as tables);
  * zero copy is the DEFAULT and copying is an explicit opt-in;
  * excluded-by-default classes must be opted IN, never silently ingested;
  * approval needs a human name, and any later edit invalidates it.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fsspec

from core.onboarding import blueprint as bp
from core.onboarding.sources import external_discovery as ed
from core.storage.external_data import ExternalDataPolicy
from core.storage.workspace_layout import WorkspaceLayout

_KEYS = [
    "/bkt/transactions/part-0.parquet",
    "/bkt/transactions/part-1.parquet",
    "/bkt/patients/patients.csv",
    "/bkt/docs/Sample_KPI.xlsx",
    "/bkt/docs/DataModel.png",
    "/bkt/logs/app.log",
]


def _workspace_with_discovery(tmp: str) -> Path:
    fs = fsspec.filesystem("memory")
    for key in _KEYS:
        with fs.open(key, "wb") as handle:
            handle.write(b"x" * 1024)
    root = Path(tmp)
    (root / "workspaces" / "w").mkdir(parents=True)
    policy = ExternalDataPolicy(configured_uri_roots=("memory://bkt/",))
    with patch.object(ed, "load_external_data_policy", return_value=policy):
        ed.ExternalSourceDiscoverer(root, "workspaces/w", "memory://bkt/").run()
    return root


def _build(root: Path, **kwargs):
    return bp.build_blueprint(
        root, "workspaces/w", source_root="s3://bkt/", catalog="cat", **kwargs
    )


def _payload(root: Path) -> dict:
    return bp.load_blueprint(WorkspaceLayout(project_root=root / "workspaces" / "w"))


def _disposition(root: Path) -> dict[str, str]:
    return {g["group"]: g["disposition"] for g in _payload(root)["groups"]}


class NothingIsCreatedTests(unittest.TestCase):
    def test_a_fresh_blueprint_is_a_draft_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            result = _build(root)
            self.assertEqual(result.status, "draft")
            md = (root / result.current_markdown_path).read_text(encoding="utf-8")
            self.assertIn("NOTHING HAS BEEN CREATED YET", md)

    def test_bootstrap_commands_are_emitted_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            cmds = "\n".join(_payload(root)["bootstrap_commands"])
            for expected in ("storage-credentials create", "external-locations create",
                             "catalogs create cat", "schemas create bronze"):
                self.assertIn(expected, cmds)

    def test_a_blueprint_without_discovery_refuses_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "w").mkdir(parents=True)
            with self.assertRaises(FileNotFoundError) as ctx:
                _build(root)
            self.assertIn("discover-external-sources", str(ctx.exception))


class DispositionTests(unittest.TestCase):
    def test_datasets_default_to_zero_copy_external_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            d = _disposition(root)
            self.assertEqual(d["transactions"], bp.DISPOSITION_EXTERNAL_TABLE)
            self.assertEqual(d["patients"], bp.DISPOSITION_EXTERNAL_TABLE)

    def test_documents_go_to_a_volume_never_a_table(self):
        # Databricks: "You can't register files in volumes as tables."
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            for group, disposition in _disposition(root).items():
                if group.startswith("docs/"):
                    self.assertEqual(disposition, bp.DISPOSITION_VOLUME)

    def test_logs_are_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            self.assertEqual(_disposition(root)["logs"], bp.DISPOSITION_EXCLUDED)

    def test_a_volume_target_is_a_volumes_path_and_a_table_target_is_three_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            targets = {g["group"]: g["target"] for g in _payload(root)["groups"]}
            self.assertEqual(targets["transactions"], "cat.bronze.transactions")
            self.assertTrue(targets["docs/Sample_KPI.xlsx"].startswith("/Volumes/cat/bronze/"))


class EditTests(unittest.TestCase):
    def test_exclude_drops_a_group_and_marks_it_as_the_users_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            _build(root, edits=bp.BlueprintEdits(exclude=["transactions"]))
            group = next(g for g in _payload(root)["groups"] if g["group"] == "transactions")
            self.assertEqual(group["disposition"], bp.DISPOSITION_EXCLUDED)
            self.assertTrue(group["edited_by_user"])
            self.assertIn("your request", group["reason"])

    def test_as_managed_opts_into_copying_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            _build(root, edits=bp.BlueprintEdits(as_managed=["patients"]))
            self.assertEqual(_disposition(root)["patients"], bp.DISPOSITION_MANAGED_TABLE)
            cmds = "\n".join(_payload(root)["bootstrap_commands"])
            self.assertIn("COPIES DATA", cmds)

    def test_include_can_opt_in_a_default_excluded_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            _build(root, edits=bp.BlueprintEdits(include=["logs"]))
            self.assertEqual(_disposition(root)["logs"], bp.DISPOSITION_EXTERNAL_TABLE)

    def test_edits_persist_across_a_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            _build(root, edits=bp.BlueprintEdits(exclude=["transactions"]))
            _build(root)  # re-run with no new edits
            self.assertEqual(_disposition(root)["transactions"], bp.DISPOSITION_EXCLUDED)

    def test_a_later_instruction_overrides_an_earlier_one_for_the_same_group(self):
        # "remove transactions" then "actually put it back" must not leave the
        # group in two buckets with the winner decided by iteration order.
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            _build(root, edits=bp.BlueprintEdits(exclude=["transactions"]))
            _build(root, edits=bp.BlueprintEdits(include=["transactions"]))
            edits = _payload(root)["edits"]
            self.assertNotIn("transactions", edits["exclude"])
            self.assertIn("transactions", edits["include"])
            self.assertEqual(_disposition(root)["transactions"], bp.DISPOSITION_EXTERNAL_TABLE)


class ApprovalTests(unittest.TestCase):
    def test_an_agent_cannot_approve_a_blueprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            for name in ("", "claude", "agent"):
                with self.subTest(name=name):
                    with self.assertRaises(PermissionError):
                        bp.approve_blueprint(root, "workspaces/w", confirmed_by=name)

    def test_a_human_approval_is_recorded_with_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            result = bp.approve_blueprint(root, "workspaces/w", confirmed_by="Shubham")
            self.assertEqual(result.status, "approved")
            approval = _payload(root)["approval"]
            self.assertEqual(approval["confirmed_by"], "Shubham")
            self.assertEqual(approval["source"], "human")
            self.assertTrue(approval["approved_at"])

    def test_editing_after_approval_invalidates_it(self):
        # Approving plan A must never carry over onto plan B.
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            bp.approve_blueprint(root, "workspaces/w", confirmed_by="Shubham")
            _build(root, edits=bp.BlueprintEdits(exclude=["patients"]))
            payload = _payload(root)
            self.assertEqual(payload["status"], "draft")
            self.assertEqual(payload["approval"], {})
            self.assertIn("changed after it was approved", payload["approval_invalidated"])

    def test_approving_nothing_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces" / "w").mkdir(parents=True)
            with self.assertRaises(FileNotFoundError):
                bp.approve_blueprint(root, "workspaces/w", confirmed_by="Shubham")


class DataModelTests(unittest.TestCase):
    def test_a_supplied_image_is_parsed_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            _build(root)
            model = _payload(root)["data_model"]
            self.assertEqual(model["action"], "parse_supplied_image")
            self.assertIn("DataModel.png", model["source"])

    def test_with_no_image_a_model_is_generated_FOR_CONFIRMATION(self):
        # The point: an assumption becomes a recorded human decision.
        discovery = {"files": [{"relative_path": "patients/p.csv", "class_name": "dataset"}]}
        model = bp._data_model_plan(discovery)
        self.assertEqual(model["action"], "generate_for_confirmation")
        self.assertIn("confirm", model["detail"].lower())


class NarrativeTests(unittest.TestCase):
    """The blueprint must SAY what it will do, not just encode it."""

    def test_it_states_where_dbt_runs_and_the_idempotency_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            result = _build(root)
            md = (root / result.current_markdown_path).read_text(encoding="utf-8")
            self.assertIn("dbt", md)
            self.assertIn("incremental_predicates", md)
            self.assertIn("unique_key", md)

    def test_it_states_the_modelling_technique_and_the_layer_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            result = _build(root)
            md = (root / result.current_markdown_path).read_text(encoding="utf-8")
            for expected in ("bronze", "silver", "gold", "star schema"):
                self.assertIn(expected, md)

    def test_it_is_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace_with_discovery(tmp)
            result = _build(root)
            md = (root / result.current_markdown_path).read_text(encoding="utf-8")
            self.assertTrue(md.isascii(), "blueprint must be ASCII (repo rule)")


if __name__ == "__main__":
    unittest.main()

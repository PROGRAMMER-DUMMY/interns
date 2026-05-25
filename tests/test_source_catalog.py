from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.onboarding.sources.catalog import SourceCatalogManager, main
from core.resource.manager import HardwareProfile, ResourceBudget


class SourceCatalogTests(unittest.TestCase):
    def _workspace(self, repo: Path) -> Path:
        workspace = repo / "workspaces" / "demo"
        (workspace / "docs").mkdir(parents=True)
        (workspace / "datasets").mkdir(parents=True)
        return workspace

    def _write_selection(self, workspace: Path, sources: list[dict]) -> Path:
        path = workspace / "docs" / "source_selection.json"
        path.write_text(json.dumps({"version": 1, "sources": sources}), encoding="utf-8")
        return path

    def _hardware(self, *, free_disk: int = 10_000_000_000) -> HardwareProfile:
        return HardwareProfile(
            cpu_count=4,
            total_memory_bytes=8_000_000_000,
            available_memory_bytes=4_000_000_000,
            workspace_disk_free_bytes=free_disk,
            workspace_disk_total_bytes=20_000_000_000,
            temp_disk_free_bytes=free_disk,
            temp_disk_total_bytes=20_000_000_000,
            platform="test",
            temp_dir="C:/tmp",
        )

    def test_plan_supports_api_local_and_databricks_sources(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            local_file = workspace / "docs" / "dictionary.csv"
            local_file.write_text("id,name\n1,a\n", encoding="utf-8")
            self._write_selection(
                workspace,
                [
                    {
                        "id": "cms_public_rows",
                        "type": "api",
                        "approval": "approved",
                        "url": "https://example.test/data",
                        "extension": "json",
                    },
                    {
                        "id": "local_dictionary",
                        "type": "local",
                        "approval": "approved",
                        "path": "docs/dictionary.csv",
                        "target_kind": "doc",
                    },
                    {
                        "id": "claims_uc",
                        "type": "databricks_uc",
                        "approval": "approved",
                        "catalog": "dev",
                        "schema": "rcm",
                        "table": "claims",
                    },
                ],
            )

            result = SourceCatalogManager(repo, "workspaces/demo").plan()

            self.assertEqual(result.source_count, 3)
            self.assertEqual([a["source_type"] for a in result.actions], ["api", "local", "databricks_uc"])
            self.assertTrue((workspace / "interns/generated/requirements/source_catalog_plan.json").exists())
            self.assertTrue((workspace / "interns/reports/source_catalog_plan.md").exists())

    def test_ingest_local_copy_writes_provenance(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            source = workspace / "docs" / "source.csv"
            source.write_text("id,value\n1,10\n", encoding="utf-8")
            self._write_selection(
                workspace,
                [
                    {
                        "id": "source_copy",
                        "type": "local",
                        "approval": "approved",
                        "path": "docs/source.csv",
                        "target_name": "copied_source",
                    }
                ],
            )

            result = SourceCatalogManager(repo, "workspaces/demo").ingest()

            action = result.actions[0]
            self.assertEqual(action["status"], "copied")
            self.assertTrue(Path(action["materialized_path"]).exists())
            self.assertTrue(Path(action["provenance_path"]).exists())

    def test_databricks_uc_without_remote_approval_is_plan_only(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            self._write_selection(
                workspace,
                [
                    {
                        "id": "claims_uc",
                        "type": "databricks_uc",
                        "approval": "approved",
                        "catalog": "dev",
                        "schema": "rcm",
                        "table": "claims",
                    }
                ],
            )

            with patch.dict("os.environ", {}, clear=True):
                result = SourceCatalogManager(repo, "workspaces/demo").ingest()

            self.assertEqual(result.actions[0]["status"], "planned_only")
            self.assertEqual(result.actions[0]["reason"], "remote_execution_not_approved")

    def test_api_ingest_writes_rows_and_provenance(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            self._write_selection(
                workspace,
                [
                    {
                        "id": "api_rows",
                        "type": "api",
                        "approval": "approved",
                        "url": "https://example.test/data",
                        "extension": "csv",
                        "max_pages": 1,
                        "page_size": 1,
                        "pagination": {"rows_key": "data"},
                    }
                ],
            )

            fetch = AsyncMock(return_value=b'{"data":[{"id":1,"value":10}]}')
            with patch("core.onboarding.sources.catalog._fetch_bytes_with_retry", fetch):
                result = SourceCatalogManager(repo, "workspaces/demo").ingest()

            action = result.actions[0]
            self.assertEqual(action["status"], "fetched")
            materialized = Path(action["materialized_path"])
            self.assertTrue(materialized.exists())
            self.assertIn("id,value", materialized.read_text(encoding="utf-8"))
            self.assertTrue((repo / action["checkpoint_path"]).exists())
            self.assertEqual(action["pages_succeeded"], 1)
            self.assertEqual(action["scheduler"]["mode"], "concurrent_api")

    def test_api_ingest_applies_multiple_sources_with_scheduler(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            self._write_selection(
                workspace,
                [
                    {
                        "id": "api_rows_a",
                        "type": "api",
                        "approval": "approved",
                        "url": "https://a.example.test/data",
                        "extension": "json",
                        "max_pages": 1,
                        "page_size": 1,
                        "fetch_policy": {"qps": 1000, "concurrency": 2},
                        "pagination": {"rows_key": "data"},
                    },
                    {
                        "id": "api_rows_b",
                        "type": "api",
                        "approval": "approved",
                        "url": "https://b.example.test/data",
                        "extension": "json",
                        "max_pages": 1,
                        "page_size": 1,
                        "fetch_policy": {"qps": 1000, "concurrency": 2},
                        "pagination": {"rows_key": "data"},
                    },
                ],
            )

            fetch = AsyncMock(side_effect=[b'{"data":[{"id":1}]}', b'{"data":[{"id":2}]}'])
            with patch("core.onboarding.sources.catalog._fetch_bytes_with_retry", fetch):
                result = SourceCatalogManager(repo, "workspaces/demo").ingest()

            self.assertEqual([action["status"] for action in result.actions], ["fetched", "fetched"])
            self.assertEqual(result.actions[0]["scheduler"]["max_concurrent"], 2)
            self.assertEqual(fetch.await_count, 2)

    def test_api_ingest_quarantines_failed_page_but_keeps_successful_rows(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            self._write_selection(
                workspace,
                [
                    {
                        "id": "api_rows",
                        "type": "api",
                        "approval": "approved",
                        "url": "https://example.test/data",
                        "extension": "json",
                        "max_pages": 2,
                        "page_size": 1,
                        "pagination": {"rows_key": "data"},
                    }
                ],
            )

            fetch = AsyncMock(
                side_effect=[
                    b'{"data":[{"id":1,"value":10}]}',
                    RuntimeError("simulated page failure"),
                ]
            )
            with patch("core.onboarding.sources.catalog._fetch_bytes_with_retry", fetch):
                result = SourceCatalogManager(repo, "workspaces/demo").ingest()

            action = result.actions[0]
            self.assertEqual(action["status"], "partial_failed")
            self.assertEqual(action["failure_count"], 1)
            self.assertTrue((repo / action["quarantine_dir"]).exists())

    def test_api_ingest_resumes_from_checkpoint(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            self._write_selection(
                workspace,
                [
                    {
                        "id": "api_rows",
                        "type": "api",
                        "approval": "approved",
                        "url": "https://example.test/data",
                        "extension": "csv",
                        "max_pages": 2,
                        "page_size": 1,
                        "pagination": {"rows_key": "data"},
                    }
                ],
            )
            target = workspace / "datasets" / "api_rows" / "data.csv"
            target.parent.mkdir(parents=True)
            target.write_text("id,value\n1,10\n", encoding="utf-8")
            checkpoint = workspace / "interns" / "state" / "source_catalog" / "checkpoints" / "api_rows.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text(json.dumps({"pages_succeeded": 1}), encoding="utf-8")

            fetch = AsyncMock(return_value=b'{"data":[{"id":2,"value":20}]}')
            with patch("core.onboarding.sources.catalog._fetch_bytes_with_retry", fetch):
                result = SourceCatalogManager(repo, "workspaces/demo").ingest()

            self.assertEqual(result.actions[0]["rows_written"], 2)
            self.assertIn("2,20", target.read_text(encoding="utf-8"))

    def test_api_ingest_schema_mismatch_quarantines(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            self._write_selection(
                workspace,
                [
                    {
                        "id": "api_rows",
                        "type": "api",
                        "approval": "approved",
                        "url": "https://example.test/data",
                        "extension": "csv",
                        "max_pages": 1,
                        "page_size": 1,
                        "expected_columns": ["id", "missing_col"],
                        "pagination": {"rows_key": "data"},
                    }
                ],
            )

            fetch = AsyncMock(return_value=b'{"data":[{"id":1,"value":10}]}')
            with patch("core.onboarding.sources.catalog._fetch_bytes_with_retry", fetch):
                result = SourceCatalogManager(repo, "workspaces/demo").ingest()

            action = result.actions[0]
            self.assertEqual(action["status"], "failed")
            self.assertEqual(action["failure_count"], 1)
            self.assertTrue((repo / action["quarantine_dir"]).exists())

    def test_preflight_and_process_write_evidence(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            source = workspace / "docs" / "source.csv"
            source.write_text("id,value\n1,10\n", encoding="utf-8")
            self._write_selection(
                workspace,
                [
                    {
                        "id": "source_copy",
                        "type": "local",
                        "approval": "approved",
                        "path": "docs/source.csv",
                        "target_name": "copied_source",
                    }
                ],
            )
            manager = SourceCatalogManager(repo, "workspaces/demo")
            preflight = manager.preflight()
            self.assertEqual(preflight.actions[0]["preflight_status"], "ok")
            self.assertIn("resource_preflight", preflight.artifacts)
            manager.ingest()
            processed = manager.process()
            action = processed.actions[0]
            self.assertEqual(action["dataset_processing_status"], "profiled")
            self.assertTrue((repo / action["profile_path"]).exists())
            self.assertTrue((repo / action["staged_parquet_path"]).exists())

    def test_api_preflight_marks_resource_budget_blocker(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            self._write_selection(
                workspace,
                [
                    {
                        "id": "api_rows",
                        "type": "api",
                        "approval": "approved",
                        "url": "https://example.test/data",
                        "max_bytes": 1_000_000_000,
                    }
                ],
            )
            hardware = self._hardware(free_disk=1_000_000_000)
            budget = ResourceBudget(disk_safety_multiplier=3.0, min_free_disk_fraction=0.15)
            with (
                patch("core.resource.manager.detect_hardware", return_value=hardware),
                patch("core.resource.manager.ResourceBudget.from_env", return_value=budget),
            ):
                preflight = SourceCatalogManager(repo, "workspaces/demo").preflight()

            self.assertEqual(preflight.actions[0]["preflight_status"], "error")
            self.assertTrue(
                any(check["name"] == "api_disk_budget" and check["status"] == "error" for check in preflight.actions[0]["preflight_checks"])
            )

    def test_subcommand_cli_can_stage_process_and_validate(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            source = workspace / "docs" / "source.csv"
            source.write_text("id,value\n1,10\n", encoding="utf-8")
            self._write_selection(
                workspace,
                [
                    {
                        "id": "source_copy",
                        "type": "local",
                        "approval": "approved",
                        "path": "docs/source.csv",
                        "target_name": "copied_source",
                    }
                ],
            )

            common = ["--repo-root", str(repo), "--workspace", "workspaces/demo"]

            self.assertEqual(main(["plan", *common]), 0)
            self.assertEqual(main(["local-stage", *common, "--source", "source_copy"]), 0)
            self.assertEqual(main(["process", *common]), 0)
            self.assertEqual(main(["validate", *common]), 0)
            self.assertTrue(
                (workspace / "interns/generated/evidence/source_catalog_validation.json").exists()
            )

    def test_catalog_index_match_and_draft_selection(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            (workspace / "datasets" / "claims").mkdir(parents=True)
            catalog_path = workspace / "datasets" / "catalog_payload" / "data.json"
            catalog_path.parent.mkdir(parents=True)
            catalog_path.write_text(
                json.dumps(
                    {
                        "dataset": [
                            {
                                "identifier": "claims_monthly",
                                "title": "Monthly Claims Extract",
                                "description": "Claims payment and denial dataset",
                                "keyword": ["claims", "denial"],
                                "distribution": [
                                    {"downloadURL": "https://example.test/claims.csv"},
                                    {"accessURL": "https://example.test/claims-api"},
                                ],
                                "describedBy": "https://example.test/claims_dictionary.pdf",
                            },
                            {
                                "identifier": "unrelated",
                                "title": "Weather Station Feed",
                                "description": "Meteorology readings",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self._write_selection(
                workspace,
                [
                    {
                        "id": "catalog_payload",
                        "type": "api",
                        "approval": "approved",
                        "url": "https://example.test/catalog",
                        "target_kind": "dataset",
                    }
                ],
            )
            manager = SourceCatalogManager(repo, "workspaces/demo")
            manager.plan()
            plan_path = workspace / "interns" / "generated" / "requirements" / "source_catalog_plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["actions"][0].update(
                {
                    "status": "fetched",
                    "materialized_path": str(catalog_path),
                    "provenance_path": str(catalog_path.with_suffix(".json.provenance.json")),
                }
            )
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

            index = manager.index_catalog("catalog_payload")
            self.assertEqual(index.actions[0]["entry_count"], 2)
            matches = manager.match_catalog("catalog_payload", keywords=["claims"], min_score=1)
            self.assertEqual(matches.source_count, 1)
            draft = manager.draft_selection("catalog_payload", min_score=1)
            self.assertTrue((workspace / "docs" / "source_selection.generated.json").exists())
            self.assertGreaterEqual(draft.source_count, 2)

    def test_catalog_index_streams_top_level_json_array(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            catalog_path = workspace / "datasets" / "catalog_payload" / "data.json"
            catalog_path.parent.mkdir(parents=True)
            catalog_path.write_text(
                json.dumps(
                    [
                        {"identifier": "claims", "title": "Claims Dataset"},
                        {"identifier": "members", "title": "Members Dataset"},
                    ]
                ),
                encoding="utf-8",
            )
            self._write_selection(
                workspace,
                [
                    {
                        "id": "catalog_payload",
                        "type": "api",
                        "approval": "approved",
                        "url": "https://example.test/catalog",
                    }
                ],
            )
            manager = SourceCatalogManager(repo, "workspaces/demo")
            manager.plan()
            plan_path = workspace / "interns" / "generated" / "requirements" / "source_catalog_plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["actions"][0].update({"status": "fetched", "materialized_path": str(catalog_path)})
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

            index = manager.index_catalog("catalog_payload", max_entries=1)

            self.assertEqual(index.actions[0]["entry_count"], 1)
            index_path = repo / index.artifacts["index"]
            self.assertEqual(len(index_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_finalize_selection_requires_explicit_approval_and_writes_backup(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            self._write_selection(workspace, [{"id": "old", "type": "local", "approval": "needs_approval"}])
            draft_path = workspace / "docs" / "source_selection.generated.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "source_catalog_id": "catalog_payload",
                        "sources": [{"id": "new_api", "type": "api", "approval": "needs_approval"}],
                    }
                ),
                encoding="utf-8",
            )
            manager = SourceCatalogManager(repo, "workspaces/demo")

            with self.assertRaises(ValueError):
                manager.finalize_selection("catalog_payload")
            result = manager.finalize_selection("catalog_payload", approve_final_preview=True)

            self.assertEqual(result.source_count, 1)
            self.assertTrue((workspace / "docs" / "source_selection.json").exists())
            self.assertIn("backup", result.artifacts)

    def test_finalize_selection_infers_source_from_draft_or_workspace(self):
        with tempfile.TemporaryDirectory() as repo_td:
            repo = Path(repo_td)
            workspace = self._workspace(repo)
            draft_path = workspace / "docs" / "source_selection.generated.json"
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text(
                json.dumps(
                    {
                        "sources": [{"id": "external_file", "type": "local", "approval": "needs_approval"}],
                    }
                ),
                encoding="utf-8",
            )
            manager = SourceCatalogManager(repo, "workspaces/demo")

            result = manager.finalize_selection(approve_final_preview=True)

            final = json.loads((workspace / "docs" / "source_selection.json").read_text(encoding="utf-8"))
            self.assertEqual(result.source_count, 1)
            self.assertEqual(final["source_catalog_id"], "demo")


if __name__ == "__main__":
    unittest.main()

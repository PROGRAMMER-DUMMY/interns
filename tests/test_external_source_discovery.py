import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.sources.external_discovery import ExternalSourceDiscoverer, main


class ExternalSourceDiscoveryTests(unittest.TestCase):
    def test_discovers_external_groups_and_drafts_source_selection(self):
        with tempfile.TemporaryDirectory() as repo_td, tempfile.TemporaryDirectory() as external_td:
            repo = Path(repo_td)
            external = Path(external_td)
            workspace = repo / "workspaces" / "cold-storage"
            workspace.mkdir(parents=True)
            raw_group = external / "raw" / "Provider File"
            raw_group.mkdir(parents=True)
            (raw_group / "provider_2025.csv").write_text("id,name\n1,a\n", encoding="utf-8")
            (raw_group / "Provider Data Dictionary.pdf").write_text("fake pdf", encoding="utf-8")
            delta_log = external / "warehouse" / "provider_delta" / "_delta_log"
            delta_log.mkdir(parents=True)
            (delta_log / "00000000000000000000.json").write_text("{}", encoding="utf-8")
            partition = external / "warehouse" / "provider_delta" / "STATE=AK"
            partition.mkdir()
            (partition / "part-00000.parquet").write_text("parquet bytes", encoding="utf-8")
            (external / "system").mkdir()
            (external / "system" / "sessions.jsonl").write_text("{}", encoding="utf-8")

            result = ExternalSourceDiscoverer(
                repo,
                "workspaces/cold-storage",
                external,
                max_files=20,
            ).run()

            self.assertEqual(result.group_count, 3)
            discovery = json.loads((repo / result.discovery_path).read_text(encoding="utf-8"))
            groups = {group["group"]: group for group in discovery["groups"]}
            self.assertEqual(groups["raw/Provider File"]["strategy"], "medallion_from_raw_csv_with_docs")
            self.assertEqual(groups["warehouse/provider_delta"]["strategy"], "delta_external_table_inspection")
            self.assertEqual(groups["system/sessions.jsonl"]["strategy"], "exclude_system_state")
            draft = json.loads((repo / result.draft_selection_path).read_text(encoding="utf-8"))
            self.assertTrue(any(source["target_kind"] == "dataset" for source in draft["sources"]))
            self.assertTrue(any(source["target_kind"] == "doc" for source in draft["sources"]))
            self.assertTrue(
                any(source["path"] == str(external / "warehouse" / "provider_delta") for source in draft["sources"])
            )
            self.assertFalse(any(source["path"].endswith("part-00000.parquet") for source in draft["sources"]))
            self.assertTrue(all(source["approval"] == "needs_approval" for source in draft["sources"]))
            self.assertTrue((repo / result.report_path).exists())

    def test_external_source_discovery_cli(self):
        with tempfile.TemporaryDirectory() as repo_td, tempfile.TemporaryDirectory() as external_td:
            repo = Path(repo_td)
            external = Path(external_td)
            (repo / "workspaces" / "cold-storage").mkdir(parents=True)
            (external / "raw" / "claims").mkdir(parents=True)
            (external / "raw" / "claims" / "claims.csv").write_text("id\n1\n", encoding="utf-8")

            code = main(
                [
                    "--repo-root",
                    str(repo),
                    "--workspace",
                    "workspaces/cold-storage",
                    "--external-root",
                    str(external),
                ]
            )

            self.assertEqual(code, 0)
            self.assertTrue(
                (
                    repo
                    / "workspaces/cold-storage/interns/generated/requirements/external_source_discovery.json"
                ).exists()
            )


if __name__ == "__main__":
    unittest.main()

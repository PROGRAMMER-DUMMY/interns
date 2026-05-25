import json
import tempfile
import unittest
from pathlib import Path

from core.context.router import ContextBudget, ContextRouter, main


class ContextRouterTests(unittest.TestCase):
    def _workspace_with_artifacts(self, root: Path) -> Path:
        workspace = root / "workspaces" / "demo"
        contracts = workspace / "interns" / "generated" / "contracts"
        profiles = workspace / "interns" / "generated" / "profiles"
        memory = workspace / "interns" / "generated" / "memory"
        evidence = workspace / "interns" / "generated" / "evidence"
        for path in (contracts, profiles, memory, evidence):
            path.mkdir(parents=True, exist_ok=True)
        contracts.joinpath("kpi_feature_mapping.json").write_text(
            json.dumps(
                {
                    "artifact_type": "kpi_feature_mapping.json",
                    "summary": {"ready_kpi_count": 1, "blocked_kpi_count": 0},
                    "kpis": [
                        {
                            "kpi_id": "kpi_001",
                            "name": "Paid amount by LOB",
                            "features": [
                                {
                                    "feature": "PaidAmount",
                                    "state": "ready",
                                    "source_columns": [
                                        {
                                            "dataset": "workspaces/demo/datasets/transactions.csv",
                                            "column": "PaidAmount",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        profiles.joinpath("profile_index.json").write_text(
            json.dumps(
                {
                    "artifact_type": "profile_index.json",
                    "profiles": [
                        {
                            "path": "workspaces/demo/datasets/transactions.csv",
                            "row_count": 2,
                            "size_bytes": 128,
                            "schema": {"PaidAmount": {"dtype": "Float64"}},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        memory.joinpath("engine_evolution.json").write_text(
            json.dumps(
                {
                    "artifact_type": "engine_evolution.json",
                    "records": [],
                    "lessons": [
                        {
                            "engine": "polars",
                            "workload_family": "aggregation_small_csv_groupby1",
                            "confidence": "medium",
                            "score": 1.5,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        memory.joinpath("evolution.md").write_text(
            "# Evolution\n\nUse Polars for small local group-bys when validation is clean.\n",
            encoding="utf-8",
        )
        evidence.joinpath("resource_preflight.json").write_text(
            json.dumps({"decision": {"status": "ok", "mode": "local_standard"}}),
            encoding="utf-8",
        )
        return workspace

    def test_context_router_builds_bounded_manifest_and_wiki(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace_with_artifacts(root)

            result = ContextRouter(root, "workspaces/demo").build(
                task="plan-source-to-target",
                budget=ContextBudget.named("small"),
            )

            manifest = json.loads((root / result.manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(manifest["task"], "plan-source-to-target")
            self.assertEqual(manifest["budget"]["name"], "small")
            self.assertGreaterEqual(manifest["summary"]["selected_sections"], 2)
            topics = {page["topic"] for page in manifest["selected_pages"]}
            self.assertIn("kpi_mapping", topics)
            self.assertIn("profiles", topics)
            self.assertTrue((root / result.index_path).exists())
            self.assertTrue((root / result.pages_path).exists())
            self.assertTrue((root / result.wiki_path).exists())

    def test_context_router_cli_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace_with_artifacts(root)

            code = main(
                [
                    "build",
                    "--repo-root",
                    str(root),
                    "--workspace",
                    "workspaces/demo",
                    "--task",
                    "plan-source-to-target",
                    "--budget",
                    "standard",
                    "--max-sections",
                    "5",
                ]
            )

            self.assertEqual(code, 0)
            self.assertTrue(
                (
                    root
                    / "workspaces/demo/interns/generated/context/manifests/plan-source-to-target_standard.json"
                ).exists()
            )


if __name__ == "__main__":
    unittest.main()

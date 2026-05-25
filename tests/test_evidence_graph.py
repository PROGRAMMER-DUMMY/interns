from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.evidence_graph import WorkspaceEvidenceGraphBuilder, WorkspaceEvidenceGraphQuery
from core.onboarding.harness.trajectory_recorder import WorkspaceTrajectoryRecorder


class WorkspaceEvidenceGraphTests(unittest.TestCase):
    def test_builds_graph_from_kpi_profiles_mapping_sql_and_trajectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            _write_graph_workspace(workspace)
            WorkspaceTrajectoryRecorder(root, "workspaces/demo").record(
                event_type="workflow_step",
                status="ok",
                summary="Prepared blocker panel.",
                artifact="workspaces/demo/interns/reports/blocker_question_panel/current.json",
            )

            result = WorkspaceEvidenceGraphBuilder(root, "workspaces/demo").build()

            self.assertTrue(result.ok)
            self.assertGreater(result.node_count, 0)
            self.assertGreater(result.edge_count, 0)
            graph_path = root / result.graph_path
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            node_ids = {node["id"] for node in graph["nodes"]}
            edge_types = {edge["type"] for edge in graph["edges"]}
            self.assertIn("kpi:kpi_001", node_ids)
            self.assertIn("feature:payer", node_ids)
            self.assertIn("column:workspaces/demo/datasets/transactions.csv.payorid", node_ids)
            self.assertIn("trajectory_event:1:workflow_step", "\n".join(sorted(node_ids)))
            self.assertIn("requires_feature", edge_types)
            self.assertIn("maps_to", edge_types)
            self.assertIn("contains_event", edge_types)
            terms = {item["term"] for item in graph["queries"]["introduced_terms"]}
            self.assertIn("Payer", terms)
            self.assertTrue((workspace / "interns" / "reports" / "evidence_graph" / "current.md").exists())

    def test_queries_term_and_feature_impact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspaces" / "demo"
            _write_graph_workspace(workspace)
            WorkspaceEvidenceGraphBuilder(root, "workspaces/demo").build()

            term = WorkspaceEvidenceGraphQuery(root, "workspaces/demo").query(term="Payer")
            feature = WorkspaceEvidenceGraphQuery(root, "workspaces/demo").query(impact_feature="Payer")

            self.assertTrue(term.ok)
            self.assertGreaterEqual(term.result_count, 1)
            self.assertTrue(feature.ok)
            self.assertGreaterEqual(feature.result_count, 1)
            self.assertEqual(feature.query_type, "impact_feature")

    def test_cli_requires_workspace(self):
        from core.onboarding import evidence_graph
        from contextlib import redirect_stderr
        from io import StringIO

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            evidence_graph.main([])

    def test_query_cli_requires_workspace_and_query(self):
        from core.onboarding import evidence_graph
        from contextlib import redirect_stderr
        from io import StringIO

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            evidence_graph.query_main([])


def _write_graph_workspace(workspace: Path) -> None:
    contracts = workspace / "interns" / "generated" / "contracts"
    profiles = workspace / "interns" / "generated" / "profiles"
    solutions = workspace / "interns" / "generated" / "solutions"
    blocker = workspace / "interns" / "reports" / "blocker_question_panel"
    contracts.mkdir(parents=True)
    profiles.mkdir(parents=True)
    solutions.mkdir(parents=True)
    blocker.mkdir(parents=True)
    _write_json(
        profiles / "profile_index.json",
        {
            "artifact_type": "profile_index.json",
            "version": 1,
            "profiles": [
                {
                    "path": "workspaces/demo/datasets/transactions.csv",
                    "schema": {
                        "PayorID": "String",
                        "PaidAmount": "Float64",
                    },
                    "row_count": 10,
                }
            ],
        },
    )
    _write_json(
        contracts / "kpi_registry.json",
        {
            "artifact_type": "kpi_registry.json",
            "version": 1,
            "kpis": [
                {
                    "kpi_id": "kpi_001",
                    "name": "Paid amount by payer",
                    "metric": "amount paid",
                    "cuts": "Payer",
                    "source": "workspaces/demo/docs/Sample KPI.xlsx",
                }
            ],
        },
    )
    _write_json(
        contracts / "kpi_feature_mapping.json",
        {
            "artifact_type": "kpi_feature_mapping.json",
            "version": 2,
            "kpis": [
                {
                    "kpi_id": "kpi_001",
                    "features": [
                        {
                            "feature": "Payer",
                            "state": "accepted",
                            "accepted_mapping": {
                                "dataset": "workspaces/demo/datasets/transactions.csv",
                                "column": "PayorID",
                            },
                        }
                    ],
                }
            ],
        },
    )
    _write_json(
        blocker / "current.json",
        {
            "artifact_type": "blocker_question_panel/current.json",
            "version": 1,
            "status": "resolved",
            "feature": "Payer",
        },
    )
    (solutions / "kpi_001.sql").write_text(
        'CREATE VIEW "kpi_001_results" AS SELECT "PayorID" AS "payer" FROM "transactions";',
        encoding="utf-8",
    )


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()

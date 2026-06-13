"""Regression: single-dataset rebuild must not clobber a populated contracts file.

Latent data-loss bug: KPI blocker-panel preparation runs the real
``RelationshipContractBuilder(...).build()``. For a SINGLE-DATASET workspace the
builder re-derives no edges (profile pairing needs >=2 datasets, no docs/diagram
/finalized model), so the freshly built ``relationships`` list is empty. If the
workspace already had a POPULATED ``relationship_contracts.json`` (e.g. a
``user_confirmed`` decision or a ``proven_data_model`` edge written earlier), the
old code overwrote it with ``relationships: []`` -- silent loss of human-confirmed
/ proven decisions.

The fix:
  - ``_preserve_user_decided_relationships`` carries forward any prior edge whose
    state is in PRESERVABLE_RELATIONSHIP_STATES (user_confirmed, rejected,
    proven_data_model) that the rebuild did not re-derive.
  - ``build()`` has a data-loss floor: an empty rebuilt list never overwrites a
    populated on-disk contracts file.

Workspace-agnostic (no domain words), local-safe, hermetic (tempdir). Runs as a
plain ``unittest`` module so it works without pytest in the venv.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.relationships.contracts import (
    RelationshipContractBuilder,
    _relationship,
)
from core.storage.workspace_layout import WorkspaceLayout


def _single_dataset_workspace(tmp_path: Path) -> tuple[Path, str, WorkspaceLayout]:
    """A workspace with exactly ONE profiled dataset -- rebuild yields no edges."""
    repo_root = tmp_path
    workspace_rel = "workspaces/single_dataset_generic"
    workspace = repo_root / workspace_rel
    workspace.mkdir(parents=True)
    layout = WorkspaceLayout(project_root=workspace)
    layout.ensure_runtime_dirs()
    (workspace / "only.csv").write_text(
        "entity_key,value\n1,a\n2,b\n3,c\n", encoding="utf-8"
    )
    profile_index = {
        "profiles": [
            {
                "path": f"{workspace_rel}/only.csv",
                "schema": {"entity_key": "Int64", "value": "String"},
                "profile_path": (
                    f"{workspace_rel}/interns/generated/profiles/only.csv.profile.json"
                ),
            }
        ]
    }
    layout.profile_index_path.write_text(
        json.dumps(profile_index, indent=2), encoding="utf-8"
    )
    return repo_root, workspace_rel, layout


def _write_populated_contracts(
    layout: WorkspaceLayout, workspace_rel: str
) -> tuple[str, str]:
    """Persist a populated contracts file with one user_confirmed + one proven edge.

    The edges reference datasets that the single-dataset rebuild will NOT
    re-derive, so they exercise the carry-forward path.
    """
    rel_user = _relationship(
        left_dataset=f"{workspace_rel}/only.csv",
        left_column="entity_key",
        right_dataset=f"{workspace_rel}/dim_a.csv",
        right_column="entity_key",
        state="user_confirmed",
        confidence=0.99,
        evidence_sources=[],
        dimension_key_unique=True,
        referential_integrity_ratio=1.0,
    )
    rel_proven = _relationship(
        left_dataset=f"{workspace_rel}/only.csv",
        left_column="value",
        right_dataset=f"{workspace_rel}/dim_b.csv",
        right_column="value",
        state="proven_data_model",
        confidence=0.9,
        evidence_sources=[],
        dimension_key_unique=True,
        referential_integrity_ratio=1.0,
    )
    contract = {
        "artifact_type": "relationship_contracts.json",
        "version": 1,
        "generated_by": "test-fixture",
        "relationships": [rel_user, rel_proven],
        "summary": {
            "relationship_count": 2,
            "executable_relationship_count": 2,
            "candidate_relationship_count": 0,
        },
    }
    layout.relationship_contracts_path.write_text(
        json.dumps(contract, indent=2) + "\n", encoding="utf-8"
    )
    return rel_user["relationship_id"], rel_proven["relationship_id"]


class SingleDatasetNoClobberTests(unittest.TestCase):
    def test_single_dataset_rebuild_preserves_populated_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_root, workspace_rel, layout = _single_dataset_workspace(tmp_path)
            user_rid, proven_rid = _write_populated_contracts(layout, workspace_rel)

            # This rebuild yields no edges for a single-dataset workspace; before
            # the fix it overwrote the file with relationships: [].
            RelationshipContractBuilder(repo_root, workspace_rel).build()

            after = json.loads(
                layout.relationship_contracts_path.read_text(encoding="utf-8")
            )
            relationships = after.get("relationships") or []
            states_by_id = {
                r.get("relationship_id"): r.get("state") for r in relationships
            }

            self.assertIn(
                user_rid,
                states_by_id,
                "user_confirmed relationship must SURVIVE a single-dataset rebuild "
                "(not be replaced by relationships: [])",
            )
            self.assertEqual(states_by_id[user_rid], "user_confirmed")

            self.assertIn(
                proven_rid,
                states_by_id,
                "proven_data_model relationship must SURVIVE a single-dataset rebuild",
            )
            self.assertEqual(states_by_id[proven_rid], "proven_data_model")

            self.assertNotEqual(
                relationships,
                [],
                "populated contracts must not be clobbered to an empty list",
            )

    def test_fresh_single_dataset_workspace_still_builds_empty(self) -> None:
        """A fresh workspace (no prior contracts) still writes a normal result."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_root, workspace_rel, layout = _single_dataset_workspace(tmp_path)

            result = RelationshipContractBuilder(repo_root, workspace_rel).build()

            self.assertTrue(
                layout.relationship_contracts_path.exists(),
                "fresh build must still emit a contracts file",
            )
            self.assertEqual(
                result.relationship_count,
                0,
                "a single-dataset workspace with no prior evidence has no edges",
            )
            after = json.loads(
                layout.relationship_contracts_path.read_text(encoding="utf-8")
            )
            self.assertEqual(after.get("relationships"), [])


if __name__ == "__main__":
    unittest.main()

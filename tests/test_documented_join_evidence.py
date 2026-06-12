"""Documented data-model joins as first-class relationship evidence (F4).

Joins that the workspace's own documentation declares -- a dictionary gloss
("joins to party.party_key") or a data-model overview table row -- must be
extracted even when the key names do not match, recorded as
``documented_data_model`` (non-executable), and promoted to
``proven_data_model`` only when observed key values prove the overlap.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.relationships.contracts import (
    DOCUMENTED_RELATIONSHIP_STATE,
    RelationshipContractBuilder,
)


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def _build_workspace(root: Path, *, dictionary: str = "", overview: str = "") -> Path:
    workspace = root / "workspaces" / "demo"
    datasets = workspace / "datasets"
    docs = workspace / "docs"
    contracts = workspace / "interns" / "generated" / "contracts"
    profiles_dir = workspace / "interns" / "generated" / "profiles"
    contracts.mkdir(parents=True)
    profiles_dir.mkdir(parents=True)
    docs.mkdir(parents=True)

    # Non-name-matched keys: orders.cust_ref -> customers.party_key.
    # orders.bad_ref deliberately lives in another key namespace (no overlap).
    _write_csv(
        datasets / "orders.csv",
        "Id,cust_ref,acct_ref,bad_ref,Amount",
        ["O1,C1,C2,X9,10", "O2,C2,C3,X8,20", "O3,C1,C1,X7,30"],
    )
    _write_csv(
        datasets / "customers.csv",
        "party_key,Id,Name",
        ["C1,LEG9,Alpha", "C2,LEG8,Beta", "C3,LEG7,Gamma"],
    )

    profile_entries = []
    for name, schema in {
        "orders": {
            "Id": "String",
            "cust_ref": "String",
            "acct_ref": "String",
            "bad_ref": "String",
            "Amount": "Float64",
        },
        "customers": {"party_key": "String", "Id": "String", "Name": "String"},
    }.items():
        profile_entries.append(
            {
                "path": f"workspaces/demo/datasets/{name}.csv",
                "row_count": 3,
                "schema": schema,
                "profile_path": (
                    f"workspaces/demo/interns/generated/profiles/{name}.profile.json"
                ),
            }
        )
    (profiles_dir / "profile_index.json").write_text(
        json.dumps(
            {
                "artifact_type": "profile_index.json",
                "version": 1,
                "generated_by": "onboard-workspace",
                "workspace": "workspaces/demo",
                "profiles": profile_entries,
            }
        ),
        encoding="utf-8",
    )

    data_models = []
    if dictionary:
        (docs / "data_dictionary.csv").write_text(dictionary, encoding="utf-8")
        data_models.append("workspaces/demo/docs/data_dictionary.csv")
    if overview:
        (docs / "data_model_overview.md").write_text(overview, encoding="utf-8")
        data_models.append("workspaces/demo/docs/data_model_overview.md")
    (contracts / "domain_model.json").write_text(
        json.dumps(
            {
                "artifact_type": "domain_model.json",
                "version": 1,
                "generated_by": "onboard-workspace",
                "workspace": "workspaces/demo",
                "datasets": [entry["path"] for entry in profile_entries],
                "data_models": data_models,
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _relationships(root: Path) -> list[dict]:
    RelationshipContractBuilder(root, "workspaces/demo").build()
    payload = json.loads(
        (
            root
            / "workspaces"
            / "demo"
            / "interns"
            / "generated"
            / "contracts"
            / "relationship_contracts.json"
        ).read_text(encoding="utf-8")
    )
    return payload["relationships"]


def _find(relationships: list[dict], left_column: str, right_column: str) -> dict | None:
    for relationship in relationships:
        if (
            relationship.get("left_column") == left_column
            and relationship.get("right_column") == right_column
        ):
            return relationship
    return None


class DictionaryQualifiedJoinTests(unittest.TestCase):
    def test_joins_to_qualified_reference_is_extracted_and_promoted(self) -> None:
        dictionary = (
            "table,column,description,data_type,source_system\n"
            "orders,cust_ref,Customer account reference; joins to customers.party_key,text,TMS\n"
            "customers,Id,Legacy CRM identifier - do not join on this,text,CRM\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_workspace(root, dictionary=dictionary)
            relationships = _relationships(root)
            edge = _find(relationships, "cust_ref", "party_key")
            self.assertIsNotNone(edge, f"documented join missing: {relationships}")
            # 100% of cust_ref values resolve to a unique party_key -> proven.
            self.assertEqual(edge["state"], "proven_data_model")
            self.assertTrue(edge["executable_usage_policy"]["allowed_in_sql_generation"])
            evidence_types = {item.get("type") for item in edge["evidence_sources"]}
            self.assertIn("documented_join", evidence_types)
            self.assertIn("profile_key_overlap", evidence_types)
            # The documented target column wins; the legacy Id column is never
            # used as the join key.
            self.assertIsNone(_find(relationships, "cust_ref", "Id"))

    def test_documented_join_with_no_value_overlap_stays_non_executable(self) -> None:
        dictionary = (
            "table,column,description,data_type,source_system\n"
            "orders,bad_ref,Legacy pointer; joins to customers.party_key,text,TMS\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_workspace(root, dictionary=dictionary)
            relationships = _relationships(root)
            edge = _find(relationships, "bad_ref", "party_key")
            self.assertIsNotNone(edge)
            self.assertEqual(edge["state"], DOCUMENTED_RELATIONSHIP_STATE)
            self.assertFalse(edge["executable_usage_policy"]["allowed_in_sql_generation"])
            ri = edge["referential_integrity_checks"]["left_key_resolution_ratio"]
            self.assertIsNotNone(ri)
            self.assertLess(ri, 0.5)


class OverviewTableJoinTests(unittest.TestCase):
    def test_master_data_table_row_documents_joins(self) -> None:
        overview = (
            "# Warehouse extract - data model overview\n"
            "\n"
            "## Master data\n"
            "\n"
            "| Table | Key | Joined from |\n"
            "|---|---|---|\n"
            "| customers.csv | `party_key` | `orders.cust_ref` and most acct columns |\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_workspace(root, overview=overview)
            relationships = _relationships(root)
            edge = _find(relationships, "cust_ref", "party_key")
            self.assertIsNotNone(edge, f"documented table join missing: {relationships}")
            self.assertEqual(edge["state"], "proven_data_model")
            evidence_types = {item.get("type") for item in edge["evidence_sources"]}
            self.assertIn("documented_join", evidence_types)

    def test_slash_expanded_column_references(self) -> None:
        overview = (
            "## Master data\n"
            "\n"
            "| Table | Key | Joined from |\n"
            "|---|---|---|\n"
            "| customers.csv | `party_key` | `orders.cust_ref`/`acct_ref` |\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_workspace(root, overview=overview)
            relationships = _relationships(root)
            first = _find(relationships, "cust_ref", "party_key")
            second = _find(relationships, "acct_ref", "party_key")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second, "slash-suffixed column reference was dropped")
            self.assertEqual(first["state"], "proven_data_model")
            self.assertEqual(second["state"], "proven_data_model")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

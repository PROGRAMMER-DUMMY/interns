"""End-to-end regression: a non-healthcare workspace must flow through
onboard → research → relationship contracts → workspace-flow status diff
without leaking healthcare vocabulary anywhere.

Uses a fully synthetic retail workspace (orders/customers/products) with
two retail KPIs. Asserts that NO generated artifact contains words from
the healthcare leak list — proves the platform is no longer centric to
the Healthcare-RCM fixture.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.onboarding.relationships.contracts import RelationshipContractBuilder
from core.onboarding.workspace.delegation import (
    record_delegation,
    verdict_from_relationship_summary,
)
from core.onboarding.workspace.research import research_workspace_vocabulary
from core.storage.workspace_layout import WorkspaceLayout


HEALTHCARE_LEAK_WORDS = {
    "medicare", "medicaid", "payor", "payer", "encounter", "claim",
    "patient", "diagnosis", "icd", "department", "hospital",
}


def _make_retail_workspace(tmp_path: Path) -> tuple[Path, str, WorkspaceLayout]:
    repo_root = tmp_path
    workspace_rel = "workspaces/synthetic_retail_e2e"
    workspace = repo_root / workspace_rel
    workspace.mkdir(parents=True)
    layout = WorkspaceLayout(project_root=workspace)
    layout.ensure_runtime_dirs()

    (workspace / "orders.csv").write_text(
        "order_id,customer_id,order_date,total_amount,channel,status\n"
        "1,7,2025-01-05,49.99,Web,Fulfilled\n"
        "2,9,2025-02-10,200.00,Mobile,Fulfilled\n"
        "3,7,2025-03-12,12.50,Store,Refunded\n",
        encoding="utf-8",
    )
    (workspace / "customers.csv").write_text(
        "customer_id,signup_date,segment\n7,2024-06-01,Loyal\n9,2024-08-15,New\n",
        encoding="utf-8",
    )
    (workspace / "data_dictionary.csv").write_text(
        "table,column,description\n"
        "orders,order_id,Primary key for the orders table.\n"
        "orders,customer_id,Foreign key to customers.customer_id.\n"
        "customers,customer_id,Primary key for the customers table.\n",
        encoding="utf-8",
    )
    profile_index = {
        "profiles": [
            {
                "path": f"{workspace_rel}/orders.csv",
                "schema": {
                    "order_id": "Int64", "customer_id": "Int64", "order_date": "Date",
                    "total_amount": "Float64", "channel": "String", "status": "String",
                },
                "profile_path": f"{workspace_rel}/interns/generated/profiles/orders.csv.profile.json",
            },
            {
                "path": f"{workspace_rel}/customers.csv",
                "schema": {"customer_id": "Int64", "signup_date": "Date", "segment": "String"},
                "profile_path": f"{workspace_rel}/interns/generated/profiles/customers.csv.profile.json",
            },
        ]
    }
    layout.profile_index_path.write_text(json.dumps(profile_index), encoding="utf-8")
    (layout.contracts_dir / "domain_model.json").write_text(
        json.dumps({"data_models": [f"{workspace_rel}/data_dictionary.csv"]}),
        encoding="utf-8",
    )
    layout.kpi_registry_path.write_text(
        json.dumps(
            {
                "kpis": [
                    {
                        "kpi_id": "kpi_001",
                        "name": "Monthly revenue trend by channel",
                        "business_question": "What is the monthly revenue trend across sales channels?",
                        "metric": "sum(total_amount)",
                        "cuts": "month, channel",
                    },
                    {
                        "kpi_id": "kpi_002",
                        "name": "Refund rate by channel",
                        "business_question": "What is the refund rate by sales channel?",
                        "metric": "count(status = 'Refunded') / count(*)",
                        "cuts": "channel",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return repo_root, workspace_rel, layout


def _scan_for_leak(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8").lower()
    except (OSError, UnicodeDecodeError):
        return []
    return sorted({word for word in HEALTHCARE_LEAK_WORDS if word in text})


def test_retail_workspace_onboards_without_leaking_healthcare_vocabulary(tmp_path):
    repo_root, workspace_rel, layout = _make_retail_workspace(tmp_path)

    # 1. Vocabulary research must produce retail-flavored terms only.
    research = research_workspace_vocabulary(repo_root, workspace_rel)
    assert research.confidence > 0
    vocab_path = layout.contracts_dir / "workspace_vocabulary.json"
    leaks = _scan_for_leak(vocab_path)
    assert not leaks, f"workspace_vocabulary leaks healthcare words: {leaks}"

    # 2. Relationship contracts built from the retail dictionary must have at least
    #    one executable relationship (orders.customer_id -> customers.customer_id).
    rel_result = RelationshipContractBuilder(repo_root, workspace_rel).build()
    assert rel_result.executable_relationship_count >= 1, (
        "data_dictionary FK description must yield at least one executable relationship"
    )
    leaks = _scan_for_leak(layout.relationship_contracts_path)
    assert not leaks, f"relationship_contracts leaks healthcare words: {leaks}"

    # 3. Delegation event must record cleanly without leaking healthcare words.
    event = record_delegation(
        layout, workspace_rel,
        agent="data-engineer",
        stage="relationship_review",
        reason="retail e2e regression",
        verdict_fn=lambda: verdict_from_relationship_summary(rel_result.summary()),
    )
    assert event.verdict.status in {"ok", "needs_review", "warning"}
    trajectory_leaks = _scan_for_leak(layout.state_dir / "trajectory.jsonl")
    assert not trajectory_leaks, f"trajectory.jsonl leaks healthcare words: {trajectory_leaks}"

    # 4. Per-KPI definitions in the registry contract must round-trip without
    #    losing or distorting retail-specific terms.
    registry_text = layout.kpi_registry_path.read_text(encoding="utf-8").lower()
    for required in ("channel", "refunded", "total_amount"):
        assert required in registry_text, f"retail term `{required}` should appear unchanged"


def test_retail_data_dictionary_drives_relationships_without_healthcare_inference(tmp_path):
    repo_root, workspace_rel, layout = _make_retail_workspace(tmp_path)
    result = RelationshipContractBuilder(repo_root, workspace_rel).build()
    contract = json.loads(layout.relationship_contracts_path.read_text(encoding="utf-8"))
    relationships = contract.get("relationships") or []
    assert relationships, "must build at least one relationship from the retail dictionary"
    for rel in relationships:
        left_dataset = str(rel.get("left_dataset", ""))
        right_dataset = str(rel.get("right_dataset", ""))
        for word in ("hospital", "patient", "encounter", "claim", "medicare", "payor"):
            assert word not in left_dataset.lower() and word not in right_dataset.lower(), (
                f"relationship references healthcare term `{word}` on a retail workspace"
            )


def test_research_writes_vocabulary_with_retail_filter_terms(tmp_path):
    repo_root, workspace_rel, layout = _make_retail_workspace(tmp_path)
    research_workspace_vocabulary(repo_root, workspace_rel)
    from core.onboarding.lexicon.vocabulary import terms_for
    filter_terms = terms_for(layout, "filter_terms")
    # KPI cuts include `status = 'Refunded'` so `refunded` should land
    assert any("refunded" in t.lower() for t in filter_terms)
    # NOT any healthcare filter
    assert not any(
        any(h in t.lower() for h in HEALTHCARE_LEAK_WORDS)
        for t in filter_terms
    )

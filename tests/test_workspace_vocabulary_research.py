"""Regression tests for workspace vocabulary research.

Locks in the "derive don't curate" contract: vocabulary terms come from
the workspace's own evidence, not curated domain lists.

Test fixture is intentionally **retail/e-commerce** (not healthcare) so a
regression where the platform leaks healthcare vocabulary into a
non-healthcare workspace fails the test loud.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.onboarding.lexicon.vocabulary import (
    GENERIC_FINANCIAL_SEED,
    GENERIC_TEMPORAL_SEED,
    terms_for,
    vocabulary_confidence,
    vocabulary_present,
)
from core.onboarding.workspace.research import (
    WorkspaceResearcher,
    research_workspace_vocabulary,
)
from core.onboarding.workspace.vocabulary_panel import (
    apply_vocabulary_confirmation_answer,
    prepare_vocabulary_confirmation_panel,
)
from core.storage.workspace_layout import WorkspaceLayout


HEALTHCARE_LEAK_WORDS = {
    "medicare", "medicaid", "patient", "encounter", "claim", "payor",
    "diagnosis", "icd", "department",
}


def _make_retail_workspace(tmp_path: Path) -> tuple[Path, str, WorkspaceLayout]:
    """Synthetic retail workspace with NO healthcare references anywhere."""
    repo_root = tmp_path
    workspace_rel = "workspaces/synthetic_retail"
    workspace = repo_root / workspace_rel
    workspace.mkdir(parents=True)
    layout = WorkspaceLayout(project_root=workspace)
    layout.ensure_runtime_dirs()

    (workspace / "orders.csv").write_text(
        "order_id,customer_id,order_date,total_amount,channel,status\n"
        "1001,7,2025-01-05,49.99,Web,Fulfilled\n"
        "1002,9,2025-01-06,12.50,Mobile,Fulfilled\n"
        "1003,7,2025-01-07,200.00,Store,Refunded\n",
        encoding="utf-8",
    )
    (workspace / "customers.csv").write_text(
        "customer_id,signup_date,segment\n"
        "7,2024-06-01,Loyal\n9,2024-08-15,New\n",
        encoding="utf-8",
    )

    profile_index = {
        "profiles": [
            {
                "path": f"{workspace_rel}/orders.csv",
                "schema": {
                    "order_id": "Int64",
                    "customer_id": "Int64",
                    "order_date": "Date",
                    "total_amount": "Float64",
                    "channel": "String",
                    "status": "String",
                },
                "columns": [
                    {"name": "order_id", "dtype": "Int64", "sample_values": [1001, 1002]},
                    {"name": "customer_id", "dtype": "Int64", "sample_values": [7, 9]},
                    {"name": "order_date", "dtype": "Date", "sample_values": ["2025-01-05"]},
                    {"name": "total_amount", "dtype": "Float64", "sample_values": [49.99, 12.50]},
                    {"name": "channel", "dtype": "String",
                     "sample_values": ["Web", "Mobile", "Store"]},
                    {"name": "status", "dtype": "String",
                     "sample_values": ["Fulfilled", "Refunded"]},
                ],
            },
            {
                "path": f"{workspace_rel}/customers.csv",
                "schema": {"customer_id": "Int64", "signup_date": "Date", "segment": "String"},
                "columns": [
                    {"name": "customer_id", "dtype": "Int64", "sample_values": [7, 9]},
                    {"name": "signup_date", "dtype": "Date", "sample_values": ["2024-06-01"]},
                    {"name": "segment", "dtype": "String", "sample_values": ["Loyal", "New"]},
                ],
            },
        ]
    }
    layout.profile_index_path.write_text(json.dumps(profile_index), encoding="utf-8")

    layout.kpi_registry_path.parent.mkdir(parents=True, exist_ok=True)
    layout.kpi_registry_path.write_text(
        json.dumps(
            {
                "kpis": [
                    {
                        "kpi_id": "kpi_001",
                        "name": "What is monthly revenue trend by channel?",
                        "business_question": "Total monthly revenue trend by sales channel",
                        "metric": "sum(total_amount)",
                        "cuts": "month, channel",
                    },
                    {
                        "kpi_id": "kpi_002",
                        "name": "Refund rate by channel",
                        "business_question": "What is refund rate by channel?",
                        "metric": "count(status = 'Refunded') / count(*)",
                        "cuts": "channel",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return repo_root, workspace_rel, layout


def test_research_derives_vocabulary_from_workspace_evidence(tmp_path):
    repo_root, workspace_rel, layout = _make_retail_workspace(tmp_path)
    result = research_workspace_vocabulary(repo_root, workspace_rel)

    assert result.workspace == "synthetic_retail"
    assert result.confidence > 0, "research must derive at least some terms"
    assert vocabulary_present(layout)

    entity_terms = {item.term for item in result.categories.get("entity_terms", [])}
    assert "order" in entity_terms or "orders" in entity_terms, (
        "must derive 'order'/'orders' entity from profile_index table name"
    )
    assert "customer" in entity_terms or "customers" in entity_terms

    temporal_terms = {item.term for item in result.categories.get("temporal_terms", [])}
    assert "order_date" in temporal_terms or "signup_date" in temporal_terms, (
        "must derive temporal terms from Date-dtype columns"
    )

    financial_terms = {item.term for item in result.categories.get("financial_terms", [])}
    assert "total_amount" in financial_terms, (
        "must derive 'total_amount' from numeric column matching financial seed"
    )


def test_research_does_not_leak_healthcare_vocabulary_into_retail_workspace(tmp_path):
    repo_root, workspace_rel, layout = _make_retail_workspace(tmp_path)
    research_workspace_vocabulary(repo_root, workspace_rel)

    all_terms = []
    for category in ("financial_terms", "temporal_terms", "entity_terms", "filter_terms"):
        all_terms.extend(terms_for(layout, category))

    leaked = [term for term in all_terms if term.lower() in HEALTHCARE_LEAK_WORDS]
    assert not leaked, (
        f"Retail workspace vocabulary leaked healthcare words: {leaked}. "
        "Curated healthcare lists must not propagate into non-healthcare workspaces."
    )


def test_vocabulary_loader_falls_back_to_generic_seed_when_no_research(tmp_path):
    repo_root, workspace_rel, layout = _make_retail_workspace(tmp_path)
    # Do NOT run research. terms_for should still return a usable starter set.
    financial = terms_for(layout, "financial_terms")
    temporal = terms_for(layout, "temporal_terms")
    assert set(financial) == set(GENERIC_FINANCIAL_SEED)
    assert set(temporal) == set(GENERIC_TEMPORAL_SEED)


def test_filter_terms_derived_from_quoted_literals_and_low_cardinality_samples(tmp_path):
    repo_root, workspace_rel, layout = _make_retail_workspace(tmp_path)
    research_workspace_vocabulary(repo_root, workspace_rel)

    filter_terms = terms_for(layout, "filter_terms")
    # `'Refunded'` appears as a quoted literal in kpi_002 cuts, and `status` is
    # a low-cardinality String column with 'Refunded' / 'Fulfilled' samples.
    assert any(term.lower() == "refunded" for term in filter_terms)


def test_vocabulary_confirmation_panel_emits_status(tmp_path):
    repo_root, workspace_rel, layout = _make_retail_workspace(tmp_path)
    panel = prepare_vocabulary_confirmation_panel(repo_root, workspace_rel)
    assert panel.category_count >= 3
    assert panel.term_count >= 5
    panel_payload = json.loads(
        (layout.reports_dir / "vocabulary_confirmation" / "current.json").read_text(encoding="utf-8")
    )
    assert panel_payload["status"] in {"needs_user_answer", "complete"}
    assert panel_payload["applies_to_artifact"] == "interns/generated/contracts/workspace_vocabulary.json"


def test_apply_vocabulary_answer_persists_accepted_terms(tmp_path):
    repo_root, workspace_rel, layout = _make_retail_workspace(tmp_path)
    research_workspace_vocabulary(repo_root, workspace_rel)

    result = apply_vocabulary_confirmation_answer(
        repo_root, workspace_rel,
        category="financial_terms",
        answer="accept_all",
    )
    assert result["category"] == "financial_terms"

    vocab_path = layout.contracts_dir / "workspace_vocabulary.json"
    data = json.loads(vocab_path.read_text(encoding="utf-8"))
    accepted = [
        term for term in data["categories"]["financial_terms"]
        if isinstance(term, dict) and term.get("accepted_by_user_at")
    ]
    assert accepted, "accept_all must stamp every term with accepted_by_user_at"


def test_apply_vocabulary_edit_replaces_category_with_custom_terms(tmp_path):
    repo_root, workspace_rel, layout = _make_retail_workspace(tmp_path)
    research_workspace_vocabulary(repo_root, workspace_rel)

    apply_vocabulary_confirmation_answer(
        repo_root, workspace_rel,
        category="filter_terms",
        answer="edit",
        custom_terms=["VIP", "Standard", "Trial"],
    )

    after = terms_for(layout, "filter_terms")
    assert set(after) == {"vip", "standard", "trial"} or set(after) == {"VIP", "Standard", "Trial"}, (
        f"edit must replace category terms; got {after}"
    )

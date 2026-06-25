"""Tests for the new preview-related Markdown sections in blocker_question_panel.

Covers `_render_markdown` rendering of:
  - feature_resolution_table
  - sample_evidence
  - kpi_preview (status: ok, empty, error, timeout)
  - per-option executed_sample (status: ok, timeout)

Also pins backwards compatibility: old panels without any of these keys must
render exactly as before, with no new section headings appearing.
"""
from __future__ import annotations

import unittest

from core.onboarding.kpi.blocker_question_panel import (
    _render_markdown,
    _render_markdown_compact,
)


def _legacy_panel() -> dict:
    """A minimal legacy-shape panel — no preview keys present."""
    return {
        "feature": "paid_amount",
        "workspace": "workspaces/demo",
        "applies_to_kpis": ["kpi_001"],
        "reuse_scope": "workspace_level",
        "answer_type": "select_physical_column_or_custom_rule",
        "blocker": "`paid_amount` is unresolved.",
        "question": "Which physical column should define `paid_amount`?",
        "recommended_option_id": "option_a",
        "recommended_answer": "Accept Option A.",
        "why": "Schema/profile alias evidence available.",
        "options": [
            {
                "option_id": "option_a",
                "label": "transactions.PaidAmount",
                "business_summary": "Use transactions.PaidAmount.",
                "json_backed": False,
            },
            {
                "option_id": "custom",
                "label": "Enter a custom definition",
                "business_summary": "Provide a different rule.",
                "json_backed": False,
            },
        ],
    }


def _preview_panel() -> dict:
    """A panel with all four new keys present."""
    panel = _legacy_panel()
    panel["feature_resolution_table"] = [
        {
            "feature": "paid_amount",
            "resolves_as": "proven_direct",
            "where_it_lands": "transactions.PaidAmount",
        },
        {
            "feature": "gender",
            "resolves_as": "proven_join",
            "where_it_lands": "patients.Gender via PatientID",
        },
        {
            "feature": "age",
            "resolves_as": "blocked_missing_evidence",
            "where_it_lands": "derived from patients.DOB",
        },
    ]
    panel["sample_evidence"] = [
        {
            "feature": "paid_amount",
            "column": "transactions.PaidAmount",
            "first_samples": ["125.0", "80.5", "220.0", "45.75", "310.25"],
        },
        {
            "feature": "service_date",
            "column": "transactions.ServiceDate",
            "first_samples": ["2024-03-15", "2024-04-02", "2024-04-18"],
        },
        {
            "feature": "gender",
            "column": "patients.Gender",
            "first_samples": [],
        },
    ]
    panel["kpi_preview"] = {
        "kpi_id": "kpi_1_medicare_paid_amount_trend",
        "assumed_option_id": "option_a",
        "caption": (
            "Preview against datasets/EMR/trendytech-hospital-a/transactions.csv; "
            "final KPI SQL will UNION all enabled sources."
        ),
        "preview_result": {
            "status": "ok",
            "sql": "SELECT month, paid_amount FROM transactions LIMIT 5",
            "duration_ms": 42,
            "rows": [
                {
                    "month": "2024-03",
                    "LineOfBusiness": "Medicare",
                    "PayorID": "PAY01",
                    "Gender": "Female",
                    "age_bucket": 67,
                    "paid_amount": 5230.50,
                }
            ],
            "columns": [
                "month",
                "LineOfBusiness",
                "PayorID",
                "Gender",
                "age_bucket",
                "paid_amount",
            ],
            "error": None,
        },
    }
    panel["options"][0]["executed_sample"] = {
        "status": "ok",
        "sql": "SELECT PatientID, DOB, YEAR(...) AS age FROM patients LIMIT 5",
        "duration_ms": 8,
        "rows": [
            {"PatientID": "HOSP1-000001", "DOB": "1937-06-04", "age": 89},
            {"PatientID": "HOSP1-000002", "DOB": "1937-06-10", "age": 89},
        ],
        "columns": ["PatientID", "DOB", "age"],
        "error": None,
    }
    return panel


class RenderMarkdownPreviewSectionsTests(unittest.TestCase):
    def test_all_four_new_sections_present(self) -> None:
        panel = _preview_panel()
        out = _render_markdown(panel)
        self.assertIn("## Feature Resolution", out)
        self.assertIn("## Sample Evidence", out)
        self.assertIn("## KPI Preview", out)
        self.assertIn("#### Sample (DuckDB, LIMIT 5)", out)

        # Feature resolution table rows
        self.assertIn("| paid_amount | proven_direct | transactions.PaidAmount |", out)
        self.assertIn("| gender | proven_join | patients.Gender via PatientID |", out)

        # Sample evidence row with samples joined by ", "
        self.assertIn(
            "| paid_amount | transactions.PaidAmount | 125.0, 80.5, 220.0, 45.75, 310.25 |",
            out,
        )
        # Empty sample list renders "(no samples)"
        self.assertIn("| gender | patients.Gender | (no samples) |", out)

        # KPI preview executed-OK chunk
        self.assertIn(
            "`kpi_1_medicare_paid_amount_trend` — preview assuming option `option_a` (the recommendation).",
            out,
        )
        self.assertIn("> Executed in 42ms via DuckDB.", out)

        # Executed sample under option_a
        self.assertIn("| PatientID | DOB | age |", out)
        self.assertIn("| HOSP1-000001 | 1937-06-04 | 89 |", out)
        self.assertIn("> Executed in 8ms.", out)

    def test_legacy_panel_unchanged(self) -> None:
        """Without any new keys, the output has none of the new headings."""
        panel = _legacy_panel()
        legacy_out = _render_markdown(panel)

        # No new headings appear
        self.assertNotIn("## Feature Resolution", legacy_out)
        self.assertNotIn("## Sample Evidence", legacy_out)
        self.assertNotIn("## KPI Preview", legacy_out)
        self.assertNotIn("#### Sample (DuckDB, LIMIT 5)", legacy_out)

        # Existing sections still present
        self.assertIn("## Required User-Facing Ask", legacy_out)
        self.assertIn("## Options", legacy_out)

        # Identity check: removing the new keys from the rich panel must produce
        # exactly the same output as the legacy panel.
        rich = _preview_panel()
        for key in ("feature_resolution_table", "sample_evidence", "kpi_preview"):
            rich.pop(key, None)
        for option in rich.get("options") or []:
            option.pop("executed_sample", None)
        self.assertEqual(_render_markdown(rich), legacy_out)

    def test_kpi_preview_error_no_table(self) -> None:
        panel = _legacy_panel()
        panel["kpi_preview"] = {
            "kpi_id": "kpi_x",
            "assumed_option_id": "option_a",
            "caption": "ignored on error",
            "preview_result": {
                "status": "error",
                "sql": "SELECT 1",
                "duration_ms": 5,
                "rows": [],
                "columns": [],
                "error": "table not found: transactions",
            },
        }
        out = _render_markdown(panel)
        self.assertIn("## KPI Preview", out)
        self.assertIn("Preview unavailable: table not found: transactions", out)
        # No data table heading row should have been built; the divider that
        # follows a header would be `| --- |` — none of our table rows render.
        self.assertNotIn("Executed in 5ms via DuckDB", out)
        # And the captioning that requires status=ok should NOT appear.
        self.assertNotIn("(the recommendation)", out)

    def test_kpi_preview_empty_zero_rows(self) -> None:
        panel = _legacy_panel()
        panel["kpi_preview"] = {
            "kpi_id": "kpi_y",
            "assumed_option_id": "option_a",
            "caption": "ignored on empty",
            "preview_result": {
                "status": "empty",
                "sql": "SELECT 1 WHERE 1=0",
                "duration_ms": 12,
                "rows": [],
                "columns": [],
                "error": None,
            },
        }
        out = _render_markdown(panel)
        self.assertIn("## KPI Preview", out)
        self.assertIn("Query executed in 12ms but returned 0 rows.", out)

    def test_executed_sample_ok_first_column_from_columns(self) -> None:
        panel = _legacy_panel()
        panel["options"][0]["executed_sample"] = {
            "status": "ok",
            "sql": "SELECT ...",
            "duration_ms": 3,
            "rows": [
                {"age": 89, "PatientID": "HOSP1-000001", "DOB": "1937-06-04"}
            ],
            "columns": ["PatientID", "DOB", "age"],
            "error": None,
        }
        out = _render_markdown(panel)
        # Header row must start with the first key in `columns`, NOT the
        # arbitrary insertion order of the row dict.
        self.assertIn("| PatientID | DOB | age |", out)
        # The row values follow that column order.
        self.assertIn("| HOSP1-000001 | 1937-06-04 | 89 |", out)
        self.assertIn("> Executed in 3ms.", out)

    def test_executed_sample_timeout_no_table(self) -> None:
        panel = _legacy_panel()
        panel["options"][0]["executed_sample"] = {
            "status": "timeout",
            "sql": "SELECT ...",
            "duration_ms": 30000,
            "rows": [],
            "columns": [],
            "error": "deadline exceeded after 30s",
        }
        out = _render_markdown(panel)
        self.assertIn("#### Sample (DuckDB, LIMIT 5)", out)
        self.assertIn("Preview unavailable: deadline exceeded after 30s", out)
        self.assertNotIn("> Executed in 30000ms.", out)


class RenderMarkdownCompactTests(unittest.TestCase):
    def _physical_panel(self) -> dict:
        panel = _legacy_panel()
        panel["workspace"] = "workspaces/Hospital_Patient_Records"
        panel["kpi_source_truth"] = [
            {
                "kpi_id": "kpi_001",
                "business_question": "How many total encounters occurred each year?",
                "metric": "count(distinct Id)",
                "cuts": ["year(START)"],
            }
        ]
        panel["options"] = [
            {
                "option_id": "option_a",
                "label": "encounters.Id",
                "business_summary": (
                    "RECOMMENDED. `encounters.Id` — PK, unique encounter id. "
                    "Sample values: 'a', 'b'. Source: `encounters.csv`."
                ),
                "evidence_summary": "PK, unique encounter id",
                "confidence": "high",
                "is_recommended": True,
                "json_backed": True,
                "physical_column_option": {
                    "observed_values": ["0002c38a", "00059b24", "00091c5b", "00092210"],
                },
            },
            {
                "option_id": "custom",
                "label": "Enter a custom definition",
                "business_summary": "Provide a different rule.",
                "json_backed": False,
            },
        ]
        return panel

    def test_compact_leads_with_kpi_and_options(self) -> None:
        out = _render_markdown_compact(self._physical_panel())
        # Header, KPI truth, decision, and options are all present and ordered.
        self.assertIn("# Blocker: paid_amount", out)
        self.assertIn("**kpi_001** — How many total encounters occurred each year?", out)
        self.assertIn("Metric: `count(distinct Id)`", out)
        self.assertIn("## Decide", out)
        self.assertIn("## Options", out)
        self.assertIn("1. [RECOMMENDED] `option_a` · encounters.Id  (confidence: high)", out)
        self.assertIn("   PK, unique encounter id", out)
        self.assertIn("   Samples: 0002c38a, 00059b24, 00091c5b", out)  # capped at 3
        self.assertIn("2. `custom` · Enter a custom definition", out)

    def test_compact_includes_apply_command(self) -> None:
        out = _render_markdown_compact(self._physical_panel())
        self.assertIn(
            "uv run apply-kpi-panel-answer "
            "--workspace workspaces/Hospital_Patient_Records "
            "--domain <domain> --answer option_a",
            out,
        )
        self.assertIn("Full proof & evidence: `current_full.md`", out)

    def test_compact_omits_machine_guidance_and_raw_json(self) -> None:
        """The compact card must not carry the agent-only contract sections or a
        raw JSON evidence dump -- those belong in current_full.md / current.json."""
        panel = self._physical_panel()
        panel["interaction_contract"] = {"instruction": "x", "display_mode": "y"}
        panel["panel_contract"] = {"display_shape": "full_kpi_truth_packet"}
        panel["output_dialect"] = {"label": "SQL (default)"}
        panel["immutable_kpi_policy"] = {"rule": "hard truth"}
        out = _render_markdown_compact(panel)
        self.assertNotIn("## Interaction Contract", out)
        self.assertNotIn("## Panel Contract", out)
        self.assertNotIn("## Output Dialect", out)
        self.assertNotIn("## Immutable KPI Policy", out)
        self.assertNotIn("## KPI Understanding Review", out)
        self.assertNotIn("```json", out)


if __name__ == "__main__":
    unittest.main()

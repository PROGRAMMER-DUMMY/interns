"""
interns/medallion_architect.py — Medallion Architect Intern.

Proposes the Bronze/Silver/Gold layered design for a workspace:
star schema (facts, dims, grain, relationships), Silver transformation
contracts (casts, dedup, PII, derived columns lifted from confirmed
derived_feature_reviews), and column-level lineage.

The agent emits a *proposal*; final ratification of grain, SCD types, and
ambiguous relationships flows through the existing blocker question panel
(per PRD Decision #2).

Heavy LLM path (when available): single-shot whole-design generation.
Light/no-LLM path (P0): structured fallback built from profiles + KPI
feature mapping + existing derived_feature_reviews. Keeps the agent
runnable offline and on light-tier models that need decomposed prompts.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from interns.base import InternBase

_SYSTEM = """You are the Medallion Architect Intern.

Your job: propose a Bronze/Silver/Gold pipeline for an analytics workspace.

You receive:
- A list of raw source datasets with profiled schemas.
- A KPI registry naming the analytics the user cares about.
- A KPI -> feature mapping showing which source columns serve which KPI feature.
- Any user-confirmed derived feature reviews (formulas already approved).
- Workspace feature definitions (cross-source identity rules, business glossary).

You produce ONE JSON object with this exact shape:

{
  "star_schema": {
    "facts": [
      {"name": "...", "grain": "one sentence", "source_silver_tables": [...],
       "measures": [...], "foreign_keys": {"local_col": "dim_table.surrogate_key"},
       "reasoning": "...", "evidence_sources": [...], "needs_user_confirmation": true}
    ],
    "dimensions": [
      {"name": "...", "natural_key": [...], "surrogate_key": "...", "scd_type": 1,
       "attributes": [...], "source_silver_tables": [...], "is_conformed": false,
       "reasoning": "...", "evidence_sources": [...], "needs_user_confirmation": true}
    ],
    "relationships": [
      {"from_table": "silver.X", "from_column": "...", "to_table": "silver.Y",
       "to_column": "...", "cardinality": "many_to_one",
       "confidence": 0.85, "evidence": [...], "needs_user_confirmation": true}
    ],
    "conformed_dimensions": [...],
    "derivation_reasoning": "...",
    "open_questions": [...]
  },
  "silver_tables": {
    "<table_name>": {
      "primary_key": ["source_system","..."],
      "type_casts": {"col": {"from":"String","to":"Date","format":"YYYY-MM-DD"}},
      "null_policies": {"col": "drop|error|default:<value>"},
      "dedup_keys": [...],
      "pii_hash_columns": [...],
      "derived_columns": {
        "<col>": {
          "formula_templates": {"duckdb_sql": "...", "spark_sql": "..."},
          "input_columns": [{"column":"...","dataset":"bronze.<t>","role":"...","dtype":"..."}],
          "business_meaning": "...",
          "reasoning": "...",
          "source_review": "<path to derived_feature_reviews/json/*.json or empty>",
          "computed_once_reused_by_kpis": [...]
        }
      }
    }
  }
}

Hard rules:
1. Multi-source datasets (e.g. same logical entity from hospital_a and hospital_b):
   Bronze keeps them separate (one table per source file). Silver unifies with a
   composite natural key `(source_system, <id>)` UNLESS workspace_feature_definitions
   declares the id globally unique.
2. Every PII column flagged in semantic_contract goes into `pii_hash_columns`.
   Never omit a known PII column.
3. Every derived feature already confirmed in derived_feature_reviews must be
   lifted into Silver as a materialized column. Do not redesign confirmed formulas.
4. Set `needs_user_confirmation: true` for: every grain declaration, every SCD type
   choice, every relationship with confidence < 0.95.
5. Return ONLY the JSON object. No prose, no markdown fences."""


class MedallionArchitectIntern(InternBase):
    name = "medallion_architect"

    def run(self, request: str, context: dict) -> str:
        prompt = self._build_user_prompt(context)
        response = self.engine.generate(
            _SYSTEM, prompt, max_tokens=self.cfg.intern_limits.max_output_tokens
        )
        if not response:
            return "Error: Medallion design failed (LLM returned no output)."
        return _strip_code_fences(response)

    def design(self, context: dict) -> dict[str, Any]:
        """
        Convenience entrypoint for the orchestrator: returns parsed JSON or
        raises ValueError if the LLM response can't be parsed.
        """
        raw = self.run("Design the medallion pipeline.", context)
        return _parse_json(raw)

    def _build_user_prompt(self, context: dict) -> str:
        parts: list[str] = []
        for key in (
            "domain_model",
            "kpi_registry",
            "kpi_feature_mapping",
            "profile_index",
            "workspace_feature_definitions",
            "semantic_contract",
            "derived_feature_reviews",
        ):
            blob = context.get(key)
            if not blob:
                continue
            if isinstance(blob, (dict, list)):
                blob_text = json.dumps(blob, indent=2, default=str)
            else:
                blob_text = str(blob)
            if len(blob_text) > 12000:
                blob_text = blob_text[:12000] + "\n... [truncated]"
            parts.append(f"=== {key} ===\n{blob_text}")
        return "\n\n".join(parts) if parts else "No workspace context provided."


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json|markdown)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _parse_json(text: str) -> dict[str, Any]:
    text = _strip_code_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # try to recover the first {...} block
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Medallion Architect output is not valid JSON: {exc}") from exc

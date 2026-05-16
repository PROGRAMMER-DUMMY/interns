"""
core/medallion/prompt_strategies.py — Heavy / Medium / Light prompt strategies (P4).

STRATEGY_VERSION must be bumped whenever prompt decomposition or schema changes.
Cache keys include this version so outputs from different strategy versions never
collide.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

STRATEGY_VERSION = "1.0"


@dataclass
class PromptStrategy:
    tier: str
    decompose: Callable[[str, dict], list[dict]]   # (task_class, context) -> [prompt_dict, ...]
    validate: Callable[[str, str], tuple[bool, str]]  # (task_class, response) -> (ok, error)
    max_retries: int


def strategy_for_tier(tier: str) -> PromptStrategy:
    if tier == "heavy":
        return heavy_strategy()
    if tier == "medium":
        return medium_strategy()
    return light_strategy()


def heavy_strategy() -> PromptStrategy:
    return PromptStrategy(
        tier="heavy",
        decompose=_heavy_single_shot,
        validate=_validate_json,
        max_retries=2,
    )


def medium_strategy() -> PromptStrategy:
    return PromptStrategy(
        tier="medium",
        decompose=_medium_per_entity,
        validate=_validate_json,
        max_retries=3,
    )


def light_strategy() -> PromptStrategy:
    return PromptStrategy(
        tier="light",
        decompose=_light_atomic,
        validate=_validate_json_strict,
        max_retries=5,
    )


# ── Decomposition functions ────────────────────────────────────────────────────

def _heavy_single_shot(task_class: str, context: dict) -> list[dict]:
    """One prompt for the entire task; freeform JSON output."""
    return [{
        "role": "user",
        "content": (
            f"Task: {task_class}\n\n"
            f"Context:\n{json.dumps(context, indent=2)}\n\n"
            "Return a single valid JSON object with your complete response."
        ),
    }]


def _medium_per_entity(task_class: str, context: dict) -> list[dict]:
    """One call per entity (fact table, dim table, Silver table, etc.)."""
    entities = _extract_entities(task_class, context)
    prompts = []
    for entity in entities:
        prompts.append({
            "role": "user",
            "content": (
                f"Task: {task_class} for entity `{entity['name']}`\n\n"
                f"Entity context:\n{json.dumps(entity, indent=2)}\n\n"
                "Return a valid JSON object with your response for this entity only."
            ),
        })
    return prompts or _heavy_single_shot(task_class, context)


def _light_atomic(task_class: str, context: dict) -> list[dict]:
    """Heavily decomposed; one call per relationship / derived column / assertion."""
    atoms = _extract_atoms(task_class, context)
    schema = _json_schema_for(task_class)
    prompts = []
    for atom in atoms:
        prompts.append({
            "role": "user",
            "content": (
                f"Task: {task_class} — atomic unit: {atom['label']}\n\n"
                f"Context:\n{json.dumps(atom, indent=2)}\n\n"
                f"Return ONLY a JSON object matching this schema:\n{json.dumps(schema, indent=2)}"
            ),
        })
    return prompts or _heavy_single_shot(task_class, context)


# ── Validation functions ───────────────────────────────────────────────────────

def _validate_json(task_class: str, response: str) -> tuple[bool, str]:
    try:
        json.loads(response.strip())
        return True, ""
    except json.JSONDecodeError as exc:
        return False, f"Invalid JSON: {exc}"


def _validate_json_strict(task_class: str, response: str) -> tuple[bool, str]:
    ok, err = _validate_json(task_class, response)
    if not ok:
        return ok, err
    schema = _json_schema_for(task_class)
    if not schema:
        return True, ""
    try:
        import jsonschema
        jsonschema.validate(json.loads(response.strip()), schema)
        return True, ""
    except Exception as exc:
        return False, f"Schema validation failed: {exc}"


def corrective_prompt(task_class: str, raw_response: str, error: str) -> dict:
    """Retry prompt injected after schema-validation failure (light tier)."""
    schema = _json_schema_for(task_class)
    return {
        "role": "user",
        "content": (
            f"Your previous output failed validation: {error}\n\n"
            f"Original response:\n{raw_response[:500]}\n\n"
            f"Return ONLY valid JSON matching:\n{json.dumps(schema, indent=2)}"
        ),
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_entities(task_class: str, context: dict) -> list[dict[str, Any]]:
    if "star_schema" in context:
        schema = context["star_schema"]
        entities = []
        for f in schema.get("facts", []):
            entities.append({"type": "fact", "name": f.get("name", ""), **f})
        for d in schema.get("dimensions", []):
            entities.append({"type": "dimension", "name": d.get("name", ""), **d})
        return entities
    return [{"name": task_class, "context": context}]


def _extract_atoms(task_class: str, context: dict) -> list[dict[str, Any]]:
    atoms: list[dict[str, Any]] = []
    if "silver_contract" in context:
        for table, tc in (context["silver_contract"] or {}).items():
            for col, dc in (tc.get("derived_columns") or {}).items():
                atoms.append({"label": f"{table}.{col}", "table": table, "column": col, **dc})
    if not atoms:
        atoms = [{"label": task_class, **context}]
    return atoms


def _json_schema_for(task_class: str) -> dict:
    schemas: dict[str, dict] = {
        "bronze_append_sql": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["sql"],
        },
        "star_schema_design": {
            "type": "object",
            "properties": {
                "facts": {"type": "array"},
                "dimensions": {"type": "array"},
                "relationships": {"type": "array"},
            },
            "required": ["facts", "dimensions"],
        },
    }
    return schemas.get(task_class, {})


def cache_key(
    *,
    system_prompt: str,
    context_excerpts: dict,
    task_class: str,
    model_tier: str,
) -> str:
    import hashlib
    payload = json.dumps(
        {
            "sp": hashlib.sha256(system_prompt.encode()).hexdigest(),
            "ctx": hashlib.sha256(
                json.dumps(context_excerpts, sort_keys=True).encode()
            ).hexdigest(),
            "tc": task_class,
            "tier": model_tier,
            "strategy_v": STRATEGY_VERSION,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()

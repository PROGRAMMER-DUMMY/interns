"""Medallion design-panel ratification (minimal slice of Priority 3.6).

`medallion design` proposes a star schema and writes every fact, dimension,
and relationship with `needs_user_confirmation: true` -- there was no way to
confirm one short of hand-editing `star_schema.json` or blanket-overriding
the whole gate with `--force-with-blockers`. This module is that missing
human step, following the same shape already proven by
`core.onboarding.kpi.phi_review_panel`.

This is deliberately NOT the full Priority 3.6 dimensional-modeling panel --
it does not reason about SCD type, grain, or history requirements; it does
not ask the Kimball-style "does it matter which version was in effect when a
historical transaction occurred" question per dimension. It is the minimal
mechanism that lets a human ratify (or defer) a proposed item with a real
name AND a real stated reason attached, so the record left behind is honest
about what was actually reviewed -- a name-only rubber stamp was rejected
specifically because it would look like a dimensional-model review in an
audit trail without ever being one. `confirmation_reasoning` is a free-text
field today; Priority 3.6 is what would replace it with structured,
generator-assisted SCD/grain reasoning.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.governance.provenance import decision_source, is_agent_confirmer
from core.medallion.contracts_md import render_star_schema_md
from core.medallion.design import _write_design_panel
from core.medallion.silver_contract import SilverContract
from core.medallion.star_schema import StarSchema
from core.observability.cost_ledger import anchored
from core.storage.workspace_layout import WorkspaceLayout

ANSWER_RATIFY = "ratify"
VALID_ANSWERS: tuple[str, ...] = (ANSWER_RATIFY,)


@dataclass(frozen=True)
class RatifyResult:
    item: str
    kind: str
    answer: str
    confirmed_by: str
    source: str
    star_schema_path: str
    design_panel_path: str
    remaining_open_count: int

    def summary(self) -> dict[str, Any]:
        return asdict(self)


def _load_star_schema(path: Path) -> StarSchema:
    if not path.exists():
        raise ValueError(f"no star_schema.json found at {path}; run `medallion design` first")
    data = json.loads(path.read_text(encoding="utf-8"))
    return StarSchema.from_dict(data)


def _find_item(schema: StarSchema, item_id: str) -> tuple[str, Any]:
    if item_id.startswith("fact:"):
        name = item_id.split(":", 1)[1]
        for f in schema.facts:
            if f.name == name:
                return "fact", f
        raise ValueError(f"no fact table named {name!r} in star_schema.json")
    if item_id.startswith("dim:"):
        name = item_id.split(":", 1)[1]
        for d in schema.dimensions:
            if d.name == name:
                return "dim", d
        raise ValueError(f"no dimension table named {name!r} in star_schema.json")
    if item_id.startswith("rel:"):
        for r in schema.relationships:
            rel_id = f"rel:{r.from_table}.{r.from_column}->{r.to_table}.{r.to_column}"
            if rel_id == item_id:
                return "rel", r
        raise ValueError(f"no relationship matching {item_id!r} in star_schema.json")
    raise ValueError(f"item id must start with 'fact:', 'dim:', or 'rel:', got {item_id!r}")


def ratify_design_panel_item(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    item: str,
    answer: str,
    confirmed_by: str,
    reasoning: str,
) -> dict[str, Any]:
    """Ratify one fact/dimension/relationship in the medallion design panel.

    Raises ValueError on a bad item id, an agent-asserted confirmer, or a
    blank reasoning string -- this gate exists so ratification records what
    was actually decided and why, not just that someone clicked yes.
    """
    if answer not in VALID_ANSWERS:
        raise ValueError(f"answer must be one of {VALID_ANSWERS!r}, got {answer!r}")
    if is_agent_confirmer(confirmed_by):
        raise ValueError(
            "design-panel ratification requires a real --confirmed-by name; "
            f"{confirmed_by!r} is agent-asserted, not accepted for this gate"
        )
    reasoning = (reasoning or "").strip()
    if not reasoning:
        raise ValueError(
            "--reasoning is required and must be non-empty: this tool records why an "
            "item was ratified, not just that it was -- a bare name-only ratification "
            "is indistinguishable from a rubber stamp in the audit trail"
        )

    repo_root = Path(repo_root).resolve()
    workspace_path = (repo_root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    medallion_dir = layout.generated_dir / "medallion"
    star_schema_path = medallion_dir / "star_schema.json"

    schema = _load_star_schema(star_schema_path)
    kind, obj = _find_item(schema, item)

    confirmed_at = datetime.now(timezone.utc).isoformat()
    obj.needs_user_confirmation = False
    obj.confirmed_by = confirmed_by
    obj.confirmed_at = confirmed_at
    obj.confirmation_reasoning = reasoning

    star_schema_path.write_text(json.dumps(schema.to_dict(), indent=2), encoding="utf-8")
    (medallion_dir / "star_schema.md").write_text(render_star_schema_md(schema), encoding="utf-8")

    silver_contract_path = medallion_dir / "silver_contract.json"
    silver_contract = SilverContract.from_dict(json.loads(silver_contract_path.read_text(encoding="utf-8")))
    design_panel_path = _write_design_panel(layout, schema, silver_contract)

    remaining = len(schema.unconfirmed_decisions())

    return {
        "item": item,
        "kind": kind,
        "answer": answer,
        "confirmed_by": confirmed_by,
        "source": decision_source(confirmed_by),
        "star_schema_path": _rel(star_schema_path, repo_root),
        "design_panel_path": _rel(Path(design_panel_path), repo_root),
        "remaining_open_count": remaining,
    }


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


@anchored("apply-design-panel-answer")
def apply_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apply-design-panel-answer")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--item", required=True, help="e.g. fact:accessorial, dim:provider, rel:silver.a.x->silver.b.y")
    parser.add_argument("--answer", required=True, choices=list(VALID_ANSWERS))
    parser.add_argument("--confirmed-by", default="")
    parser.add_argument("--reasoning", default="", help="Why this item is ratified (required, non-empty).")
    args = parser.parse_args(argv)
    try:
        result = ratify_design_panel_item(
            args.repo_root,
            args.workspace,
            item=args.item,
            answer=args.answer,
            confirmed_by=args.confirmed_by,
            reasoning=args.reasoning,
        )
    except ValueError as exc:
        print(f"[apply-design-panel-answer] ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


__all__ = [
    "ANSWER_RATIFY",
    "VALID_ANSWERS",
    "RatifyResult",
    "apply_main",
    "ratify_design_panel_item",
]

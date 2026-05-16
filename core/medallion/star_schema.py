"""
core/medallion/star_schema.py — Star schema contract.

Written to `workspaces/<ws>/interns/generated/medallion/star_schema.json`
(+ paired star_schema.md, regenerated). Authoritative source for fact tables,
dimension tables, grain declarations, and inter-table relationships.

Per PRD Decision #2: the agent PROPOSES this; the user ratifies grain/SCD
decisions through the existing blocker question panel before any Silver/Gold
SQL is committed.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class FactTable:
    name: str
    grain: str                                  # one-sentence grain declaration
    source_silver_tables: list[str] = field(default_factory=list)
    measures: list[str] = field(default_factory=list)
    foreign_keys: dict[str, str] = field(default_factory=dict)  # local_col -> "dim_table.surrogate_key"
    reasoning: str = ""                         # why this grain, why these measures
    evidence_sources: list[str] = field(default_factory=list)
    needs_user_confirmation: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FactTable":
        return cls(
            name=d["name"],
            grain=d["grain"],
            source_silver_tables=list(d.get("source_silver_tables", [])),
            measures=list(d.get("measures", [])),
            foreign_keys=dict(d.get("foreign_keys", {})),
            reasoning=d.get("reasoning", ""),
            evidence_sources=list(d.get("evidence_sources", [])),
            needs_user_confirmation=bool(d.get("needs_user_confirmation", True)),
        )


@dataclass
class DimensionTable:
    name: str
    source_silver_tables: list[str] = field(default_factory=list)
    natural_key: list[str] = field(default_factory=list)
    surrogate_key: str = ""
    scd_type: int = 1                           # 1 = overwrite, 2 = history
    attributes: list[str] = field(default_factory=list)
    is_conformed: bool = False
    reasoning: str = ""
    evidence_sources: list[str] = field(default_factory=list)
    needs_user_confirmation: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DimensionTable":
        return cls(
            name=d["name"],
            source_silver_tables=list(d.get("source_silver_tables", [])),
            natural_key=list(d.get("natural_key", [])),
            surrogate_key=d.get("surrogate_key", ""),
            scd_type=int(d.get("scd_type", 1)),
            attributes=list(d.get("attributes", [])),
            is_conformed=bool(d.get("is_conformed", False)),
            reasoning=d.get("reasoning", ""),
            evidence_sources=list(d.get("evidence_sources", [])),
            needs_user_confirmation=bool(d.get("needs_user_confirmation", True)),
        )


@dataclass
class Relationship:
    """Inter-table relationship inferred from evidence (profile overlap, name match, KPI joins)."""
    from_table: str           # e.g. "silver.encounter"
    from_column: str
    to_table: str             # e.g. "silver.patient"
    to_column: str
    cardinality: str = "many_to_one"            # "one_to_one" | "many_to_one" | "many_to_many"
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)   # ["value_overlap_rate=0.987", "kpi_002 joins these"]
    needs_user_confirmation: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Relationship":
        return cls(
            from_table=d["from_table"],
            from_column=d["from_column"],
            to_table=d["to_table"],
            to_column=d["to_column"],
            cardinality=d.get("cardinality", "many_to_one"),
            confidence=float(d.get("confidence", 0.0)),
            evidence=list(d.get("evidence", [])),
            needs_user_confirmation=bool(d.get("needs_user_confirmation", True)),
        )


@dataclass
class StarSchema:
    workspace: str
    facts: list[FactTable] = field(default_factory=list)
    dimensions: list[DimensionTable] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    conformed_dimensions: list[str] = field(default_factory=list)
    derivation_reasoning: str = ""
    open_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "facts": [f.to_dict() for f in self.facts],
            "dimensions": [d.to_dict() for d in self.dimensions],
            "relationships": [r.to_dict() for r in self.relationships],
            "conformed_dimensions": list(self.conformed_dimensions),
            "derivation_reasoning": self.derivation_reasoning,
            "open_questions": list(self.open_questions),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StarSchema":
        return cls(
            workspace=d["workspace"],
            facts=[FactTable.from_dict(x) for x in d.get("facts", [])],
            dimensions=[DimensionTable.from_dict(x) for x in d.get("dimensions", [])],
            relationships=[Relationship.from_dict(x) for x in d.get("relationships", [])],
            conformed_dimensions=list(d.get("conformed_dimensions", [])),
            derivation_reasoning=d.get("derivation_reasoning", ""),
            open_questions=list(d.get("open_questions", [])),
        )

    def unconfirmed_decisions(self) -> list[str]:
        """Items that need ratification through the blocker panel (PRD Decision #2)."""
        out: list[str] = []
        for f in self.facts:
            if f.needs_user_confirmation:
                out.append(f"fact:{f.name}")
        for d in self.dimensions:
            if d.needs_user_confirmation:
                out.append(f"dim:{d.name}")
        for r in self.relationships:
            if r.needs_user_confirmation:
                out.append(f"rel:{r.from_table}.{r.from_column}->{r.to_table}.{r.to_column}")
        return out

"""
core/medallion/lineage.py — Column-level lineage graph.

Written to `workspaces/<ws>/interns/generated/medallion/lineage.json`
(+ paired lineage.md). Every node is (layer, table) with its column set;
every edge declares which target columns are derived from which source
columns and by what transform.

Per Decision #13 this is the auditable "where did this Gold column come
from" surface — load-bearing for healthcare debugging.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class LineageNode:
    layer: str                # "bronze" | "silver" | "gold"
    table: str
    columns: list[str] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.layer}.{self.table}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LineageNode":
        return cls(
            layer=d["layer"],
            table=d["table"],
            columns=list(d.get("columns", [])),
        )


@dataclass
class LineageEdge:
    from_node: str            # qualified node id e.g. "bronze.patients__hospital_a"
    from_columns: list[str]
    to_node: str
    to_columns: list[str]
    transform_type: str = "passthrough"
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LineageEdge":
        return cls(
            from_node=d["from_node"],
            from_columns=list(d.get("from_columns", [])),
            to_node=d["to_node"],
            to_columns=list(d.get("to_columns", [])),
            transform_type=d.get("transform_type", "passthrough"),
            reasoning=d.get("reasoning", ""),
        )


@dataclass
class Lineage:
    workspace: str
    nodes: list[LineageNode] = field(default_factory=list)
    edges: list[LineageEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lineage":
        return cls(
            workspace=d["workspace"],
            nodes=[LineageNode.from_dict(x) for x in d.get("nodes", [])],
            edges=[LineageEdge.from_dict(x) for x in d.get("edges", [])],
        )

    def trace_to_sources(self, layer: str, table: str, column: str) -> list[tuple[str, str]]:
        """Walk edges backwards from (layer.table.column) to all bronze sources."""
        target = f"{layer}.{table}"
        seen: set[tuple[str, str]] = set()
        frontier: list[tuple[str, str]] = [(target, column)]
        sources: list[tuple[str, str]] = []
        while frontier:
            node, col = frontier.pop()
            if (node, col) in seen:
                continue
            seen.add((node, col))
            if node.startswith("bronze."):
                sources.append((node, col))
                continue
            for edge in self.edges:
                if edge.to_node == node and col in edge.to_columns:
                    for src_col in edge.from_columns:
                        frontier.append((edge.from_node, src_col))
        return sources

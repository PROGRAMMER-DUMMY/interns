"""
core/medallion — Medallion Architect agent: Bronze/Silver/Gold pipeline design.

Public surface:
    Manifest, BronzeTable, SilverTable, GoldTable, Budget, KpiRegeneration
    StarSchema, FactTable, DimensionTable, Relationship
    SilverContract, TableContract, DerivedColumn, Assertion, TypeCast, NullPolicy
    Lineage, LineageNode, LineageEdge

Design entrypoints live in core.medallion.design.
JSON↔MD rendering lives in core.medallion.contracts_md.
"""
from core.medallion.manifest import (
    Manifest,
    BronzeTable,
    SilverTable,
    GoldTable,
    Budget,
    KpiRegeneration,
    SCHEMA_VERSION,
)
from core.medallion.star_schema import (
    StarSchema,
    FactTable,
    DimensionTable,
    Relationship,
)
from core.medallion.silver_contract import (
    SilverContract,
    TableContract,
    DerivedColumn,
    Assertion,
    TypeCast,
    NullPolicy,
    FormulaTemplates,
    DerivedInputColumn,
)
from core.medallion.lineage import (
    Lineage,
    LineageNode,
    LineageEdge,
)

__all__ = [
    "Manifest", "BronzeTable", "SilverTable", "GoldTable", "Budget", "KpiRegeneration",
    "SCHEMA_VERSION",
    "StarSchema", "FactTable", "DimensionTable", "Relationship",
    "SilverContract", "TableContract", "DerivedColumn", "Assertion",
    "TypeCast", "NullPolicy", "FormulaTemplates", "DerivedInputColumn",
    "Lineage", "LineageNode", "LineageEdge",
]

"""
core/medallion/silver_contract.py — Silver-layer transformation contract.

Written to `workspaces/<ws>/interns/generated/medallion/silver_contract.json`
(+ paired silver_contract.md). Per Decision #5 the agent declares per-table
transformation rules (casts, null policies, dedup keys, PII masking, derived
columns) and post-load assertions. Assertion failures route through the
Governor as SILVER_ASSERTION_FAILED.

`DerivedColumn` deliberately reuses the shape of the existing
`derived_feature_options` block in `interns/reports/derived_feature_reviews/json/`
so user-confirmed features lift into Silver without restating evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ── type casts ─────────────────────────────────────────────────────────────────

@dataclass
class TypeCast:
    from_type: str
    to_type: str
    format: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"from": self.from_type, "to": self.to_type}
        if self.format:
            d["format"] = self.format
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TypeCast":
        return cls(
            from_type=d["from"],
            to_type=d["to"],
            format=d.get("format"),
        )


# ── null policies ──────────────────────────────────────────────────────────────

@dataclass
class NullPolicy:
    """
    One of:
      - drop                       → drop rows with null in this column
      - error                      → fail Silver load if any null appears
      - default:<value>            → coalesce to literal value
    """
    policy: str

    def to_str(self) -> str:
        return self.policy

    @classmethod
    def from_str(cls, s: str) -> "NullPolicy":
        return cls(policy=s)


# ── derived columns (reuses derived_feature_reviews shape) ─────────────────────

@dataclass
class FormulaTemplates:
    """Per-dialect formula bodies. At least one of these must be non-empty."""
    duckdb_sql: Optional[str] = None
    spark_sql: Optional[str] = None
    polars: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.duckdb_sql is not None:
            out["duckdb_sql"] = self.duckdb_sql
        if self.spark_sql is not None:
            out["spark_sql"] = self.spark_sql
        if self.polars is not None:
            out["polars"] = self.polars
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FormulaTemplates":
        return cls(
            duckdb_sql=d.get("duckdb_sql"),
            spark_sql=d.get("spark_sql"),
            polars=d.get("polars"),
        )


@dataclass
class DerivedInputColumn:
    column: str
    dataset: str           # bronze.<table> | silver.<table>
    role: str              # e.g. "birth_date", "anchor_date"
    dtype: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DerivedInputColumn":
        return cls(
            column=d["column"],
            dataset=d["dataset"],
            role=d.get("role", ""),
            dtype=d.get("dtype", ""),
        )


@dataclass
class DerivedColumn:
    name: str
    formula_templates: FormulaTemplates
    input_columns: list[DerivedInputColumn] = field(default_factory=list)
    business_meaning: str = ""
    reasoning: str = ""
    source_review: str = ""                              # path to derived_feature_reviews/json/*.json
    materialized_at_layer: str = "silver"
    computed_once_reused_by_kpis: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula_templates": self.formula_templates.to_dict(),
            "input_columns": [c.to_dict() for c in self.input_columns],
            "business_meaning": self.business_meaning,
            "reasoning": self.reasoning,
            "source_review": self.source_review,
            "materialized_at_layer": self.materialized_at_layer,
            "computed_once_reused_by_kpis": list(self.computed_once_reused_by_kpis),
        }

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "DerivedColumn":
        return cls(
            name=name,
            formula_templates=FormulaTemplates.from_dict(d.get("formula_templates", {})),
            input_columns=[DerivedInputColumn.from_dict(x) for x in d.get("input_columns", [])],
            business_meaning=d.get("business_meaning", ""),
            reasoning=d.get("reasoning", ""),
            source_review=d.get("source_review", ""),
            materialized_at_layer=d.get("materialized_at_layer", "silver"),
            computed_once_reused_by_kpis=list(d.get("computed_once_reused_by_kpis", [])),
        )


# ── assertions ─────────────────────────────────────────────────────────────────

@dataclass
class Assertion:
    """
    Post-Silver-load check. Types:
      - not_null           — columns must be non-null
      - unique             — columns must be unique
      - row_count_delta    — row count vs. prior load within tolerance_pct
      - referential_integrity — child column values exist in parent column
      - sql_plan_property  — SQL-lint property (Section 18 in the PRD)
    """
    id: str
    type: str
    columns: list[str] = field(default_factory=list)
    tolerance_pct: Optional[float] = None
    child: Optional[str] = None                     # for referential_integrity
    parent: Optional[str] = None
    property: Optional[str] = None                  # for sql_plan_property
    min_value: Optional[float] = None               # for range (accuracy)
    max_value: Optional[float] = None               # for range (accuracy)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type}
        if self.columns:
            out["columns"] = list(self.columns)
        if self.tolerance_pct is not None:
            out["tolerance_pct"] = self.tolerance_pct
        if self.child is not None:
            out["child"] = self.child
        if self.parent is not None:
            out["parent"] = self.parent
        if self.property is not None:
            out["property"] = self.property
        if self.min_value is not None:
            out["min_value"] = self.min_value
        if self.max_value is not None:
            out["max_value"] = self.max_value
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Assertion":
        return cls(
            id=d["id"],
            type=d["type"],
            columns=list(d.get("columns", [])),
            tolerance_pct=d.get("tolerance_pct"),
            child=d.get("child"),
            parent=d.get("parent"),
            property=d.get("property"),
            min_value=d.get("min_value"),
            max_value=d.get("max_value"),
        )


# ── per-table contract ─────────────────────────────────────────────────────────

@dataclass
class TableContract:
    type_casts: dict[str, TypeCast] = field(default_factory=dict)
    null_policies: dict[str, NullPolicy] = field(default_factory=dict)
    dedup_keys: list[str] = field(default_factory=list)
    pii_hash_columns: list[str] = field(default_factory=list)
    hash_salt_ref: str = "workspace.medallion_salt"
    derived_columns: dict[str, DerivedColumn] = field(default_factory=dict)
    assertions: list[Assertion] = field(default_factory=list)
    # Canonical primary-key rename: raw source column -> canonical name, e.g.
    # {"Id": "encounter_id"}. The silver projection emits `<raw> AS <canonical>`
    # so the PK the dedup_keys / MERGE / assertions reference actually exists.
    key_rename: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type_casts": {col: tc.to_dict() for col, tc in self.type_casts.items()},
            "null_policies": {col: np.to_str() for col, np in self.null_policies.items()},
            "dedup_keys": list(self.dedup_keys),
            "pii_hash_columns": list(self.pii_hash_columns),
            "hash_salt_ref": self.hash_salt_ref,
            "key_rename": dict(self.key_rename),
            "derived_columns": {n: dc.to_dict() for n, dc in self.derived_columns.items()},
            "assertions": [a.to_dict() for a in self.assertions],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TableContract":
        return cls(
            type_casts={col: TypeCast.from_dict(v) for col, v in d.get("type_casts", {}).items()},
            null_policies={col: NullPolicy.from_str(v) for col, v in d.get("null_policies", {}).items()},
            dedup_keys=list(d.get("dedup_keys", [])),
            pii_hash_columns=list(d.get("pii_hash_columns", [])),
            hash_salt_ref=d.get("hash_salt_ref", "workspace.medallion_salt"),
            key_rename=dict(d.get("key_rename", {})),
            derived_columns={n: DerivedColumn.from_dict(n, v) for n, v in d.get("derived_columns", {}).items()},
            assertions=[Assertion.from_dict(a) for a in d.get("assertions", [])],
        )


@dataclass
class SilverContract:
    workspace: str
    tables: dict[str, TableContract] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"workspace": self.workspace}
        for name, tc in self.tables.items():
            out[name] = tc.to_dict()
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SilverContract":
        ws = d.get("workspace", "")
        tables: dict[str, TableContract] = {}
        for k, v in d.items():
            if k == "workspace":
                continue
            tables[k] = TableContract.from_dict(v)
        return cls(workspace=ws, tables=tables)

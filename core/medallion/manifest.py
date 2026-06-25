"""
core/medallion/manifest.py — Manifest schema for the Medallion Architect.

The manifest is the top-level declarative artifact written to
`workspaces/<ws>/interns/generated/medallion/manifest.yaml`. It declares
target substrate, budget, three layers (Bronze/Silver/Gold), and the
KPI-regeneration block. Substrate-specific SQL/PySpark files live next to it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1


@dataclass
class Budget:
    max_usd_per_run: float = 5.00
    cache_dir: str = "interns/state/medallion/medallion_cache"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Budget":
        return cls(
            max_usd_per_run=float(d.get("max_usd_per_run", 5.00)),
            cache_dir=str(d.get("cache_dir", "interns/state/medallion/medallion_cache")),
        )


@dataclass
class BronzeTable:
    name: str
    source_file: str
    source_system: str
    load_strategy: str = "append_watermarked"
    watermark_column: Optional[str] = None
    natural_key: list[str] = field(default_factory=list)
    pii_columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BronzeTable":
        return cls(
            name=d["name"],
            source_file=d["source_file"],
            source_system=d["source_system"],
            load_strategy=d.get("load_strategy", "append_watermarked"),
            watermark_column=d.get("watermark_column"),
            natural_key=list(d.get("natural_key", [])),
            pii_columns=list(d.get("pii_columns", [])),
        )


@dataclass
class SilverTable:
    name: str
    derived_from: list[str]
    primary_key: list[str]
    load_strategy: str = "merge_on_pk"
    contract: str = ""  # relative path or fragment pointer

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SilverTable":
        return cls(
            name=d["name"],
            derived_from=list(d.get("derived_from", [])),
            primary_key=list(d.get("primary_key", [])),
            load_strategy=d.get("load_strategy", "merge_on_pk"),
            contract=d.get("contract", ""),
        )


@dataclass
class GoldTable:
    name: str
    kind: str  # "fact" | "dimension"
    derived_from: list[str]
    load_strategy: str = "full_refresh"
    grain: Optional[str] = None         # required for facts
    scd_type: Optional[int] = None      # required for dimensions (1 or 2)
    # OBT join spec for a fact: each entry pre-joins a dimension into the wide
    # gold table -> {"from_column","to_table","to_column"}. Empty = no join
    # (degenerate single-table fact).
    foreign_keys: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GoldTable":
        return cls(
            name=d["name"],
            kind=d["kind"],
            derived_from=list(d.get("derived_from", [])),
            load_strategy=d.get("load_strategy", "full_refresh"),
            grain=d.get("grain"),
            scd_type=d.get("scd_type"),
            foreign_keys=list(d.get("foreign_keys", [])),
        )


@dataclass
class KpiRegeneration:
    enabled: bool = True
    target_file: str = "interns/generated/solutions/kpi_metrics_v2.sql"
    row_equality_check_against: str = "interns/generated/solutions/kpi_metrics.sql"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KpiRegeneration":
        return cls(
            enabled=bool(d.get("enabled", True)),
            target_file=d.get("target_file", "interns/generated/solutions/kpi_metrics_v2.sql"),
            row_equality_check_against=d.get(
                "row_equality_check_against",
                "interns/generated/solutions/kpi_metrics.sql",
            ),
        )


@dataclass
class Manifest:
    workspace: str
    inputs_hash: str
    target: str = "auto"                # "duckdb" | "delta" | "auto"
    strict_databricks: bool = False
    schema_version: int = SCHEMA_VERSION
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    budget: Budget = field(default_factory=Budget)
    bronze: list[BronzeTable] = field(default_factory=list)
    silver: list[SilverTable] = field(default_factory=list)
    gold: list[GoldTable] = field(default_factory=list)
    kpi_regeneration: KpiRegeneration = field(default_factory=KpiRegeneration)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace": self.workspace,
            "generated_at": self.generated_at,
            "inputs_hash": self.inputs_hash,
            "target": self.target,
            "strict_databricks": self.strict_databricks,
            "budget": self.budget.to_dict(),
            "layers": {
                "bronze": [t.to_dict() for t in self.bronze],
                "silver": [t.to_dict() for t in self.silver],
                "gold":   [t.to_dict() for t in self.gold],
            },
            "kpi_regeneration": self.kpi_regeneration.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Manifest":
        layers = d.get("layers", {})
        bronze_entries = layers.get("bronze") or []
        silver_entries = layers.get("silver") or []
        gold_entries = layers.get("gold") or []
        return cls(
            workspace=d["workspace"],
            inputs_hash=d["inputs_hash"],
            target=d.get("target", "auto"),
            strict_databricks=bool(d.get("strict_databricks", False)),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            generated_at=d.get("generated_at", datetime.now(timezone.utc).isoformat()),
            budget=Budget.from_dict(d.get("budget", {})),
            bronze=[BronzeTable.from_dict(x) for x in bronze_entries],
            silver=[SilverTable.from_dict(x) for x in silver_entries],
            gold=[GoldTable.from_dict(x) for x in gold_entries],
            kpi_regeneration=KpiRegeneration.from_dict(d.get("kpi_regeneration", {})),
        )


def compute_inputs_hash(input_paths: list[Path]) -> str:
    """
    Content hash of (sorted relative path + sha256 of file bytes) for every input.
    Used to make design-medallion idempotent on unchanged inputs.
    """
    sha = hashlib.sha256()
    for p in sorted(input_paths, key=lambda x: str(x)):
        if not p.exists():
            continue
        sha.update(str(p).encode("utf-8"))
        sha.update(b"\x00")
        sha.update(hashlib.sha256(p.read_bytes()).digest())
        sha.update(b"\x00")
    return "sha256:" + sha.hexdigest()


def manifest_to_yaml(manifest: Manifest) -> str:
    """
    Emit YAML without a PyYAML dependency. The manifest schema is constrained
    enough that a deterministic, indentation-only emitter is correct and avoids
    a new dep. JSON is also valid YAML for the value side, but we want
    human-readable block style for the layer lists.
    """
    lines: list[str] = []
    d = manifest.to_dict()
    lines.append(f"schema_version: {d['schema_version']}")
    lines.append(f"workspace: {_yaml_scalar(d['workspace'])}")
    lines.append(f"generated_at: {_yaml_scalar(d['generated_at'])}")
    lines.append(f"inputs_hash: {_yaml_scalar(d['inputs_hash'])}")
    lines.append(f"target: {_yaml_scalar(d['target'])}")
    lines.append(f"strict_databricks: {str(d['strict_databricks']).lower()}")
    lines.append("budget:")
    for k, v in d["budget"].items():
        lines.append(f"  {k}: {_yaml_scalar(v)}")
    lines.append("layers:")
    for layer in ("bronze", "silver", "gold"):
        lines.append(f"  {layer}:")
        for entry in d["layers"][layer]:
            lines.extend(_yaml_list_entry(entry, indent=4))
    lines.append("kpi_regeneration:")
    for k, v in d["kpi_regeneration"].items():
        lines.append(f"  {k}: {_yaml_scalar(v)}")
    return "\n".join(lines) + "\n"


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    s = str(v)
    if any(ch in s for ch in [":", "#", "{", "}", "[", "]", ","]) or s != s.strip():
        return json.dumps(s)
    return s


def _yaml_list_entry(entry: dict[str, Any], indent: int) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    first = True
    for k, v in entry.items():
        prefix = "- " if first else "  "
        first = False
        if isinstance(v, list):
            if not v:
                lines.append(f"{pad}{prefix}{k}: []")
            else:
                lines.append(f"{pad}{prefix}{k}:")
                for item in v:
                    lines.append(f"{pad}    - {_yaml_scalar(item)}")
        else:
            lines.append(f"{pad}{prefix}{k}: {_yaml_scalar(v)}")
    return lines

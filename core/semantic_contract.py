"""
Semantic contracts convert KPI registries and data models into optimizer guardrails.

The contract is deliberately lightweight: it captures machine-readable schema rules
plus source documents that humans can audit. Evaluators/profilers enforce the rules.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ContractRule:
    rule_id: str
    kind: str
    description: str
    severity: str = "error"
    source: str = ""


@dataclass
class SemanticContract:
    name: str
    columns: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    kpis: dict[str, str] = field(default_factory=dict)
    rules: list[ContractRule] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @classmethod
    def from_task(cls, task: dict, root: Path) -> "SemanticContract":
        contract = cls(name=task.get("id", "optimization_task"))
        spec = task.get("semantic_contract", {}) or {}

        methodology_path = spec.get("methodology_json")
        if methodology_path:
            contract.merge_methodology_json(root / methodology_path)

        kpi_registry = spec.get("kpi_registry")
        if kpi_registry:
            contract.merge_kpi_registry(root / kpi_registry)

        for rule in spec.get("rules", []):
            contract.rules.append(
                ContractRule(
                    rule_id=rule.get("id", f"rule_{len(contract.rules) + 1}"),
                    kind=rule.get("kind", "business_rule"),
                    description=rule.get("description", ""),
                    severity=rule.get("severity", "error"),
                    source=rule.get("source", "task"),
                )
            )

        return contract

    def merge_methodology_json(self, path: Path) -> None:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        self.columns.update(data.get("columns", {}) or {})
        self.relationships.extend(data.get("relationships", []) or [])
        self.sources.append(str(path))

        for column, detail in self.columns.items():
            if detail.get("is_primary_key"):
                self.rules.append(
                    ContractRule(
                        rule_id=f"primary_key_{column}",
                        kind="primary_key",
                        description=f"{column} must remain unique and non-null at the output grain.",
                        source=str(path),
                    )
                )

    def merge_kpi_registry(self, path: Path) -> None:
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        self.sources.append(str(path))

        for name, formula in _extract_kpi_formulas(text).items():
            self.kpis[name] = formula
            self.rules.append(
                ContractRule(
                    rule_id=f"kpi_{_slug(name)}",
                    kind="kpi_formula",
                    description=f"Preserve KPI formula: {name} = {formula}",
                    source=str(path),
                )
            )

        for idx, description in enumerate(_extract_guardrails(text), 1):
            self.rules.append(
                ContractRule(
                    rule_id=f"registry_guardrail_{idx}",
                    kind="business_guardrail",
                    description=description,
                    source=str(path),
                )
            )

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_count": len(self.sources),
            "column_count": len(self.columns),
            "relationship_count": len(self.relationships),
            "kpi_count": len(self.kpis),
            "rule_count": len(self.rules),
            "rules": [rule.__dict__ for rule in self.rules[:20]],
        }


def _extract_kpi_formulas(text: str) -> dict[str, str]:
    formulas: dict[str, str] = {}
    for line in text.splitlines():
        if "|" not in line or "`" not in line:
            continue
        cells = [cell.strip(" *") for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0].strip()
        match = re.search(r"`([^`]+)`", line)
        if name and match and not name.lower().startswith(":---"):
            formulas[name] = match.group(1).strip()
    return formulas


def _extract_guardrails(text: str) -> list[str]:
    guardrails: list[str] = []
    in_guardrails = False
    for line in text.splitlines():
        stripped = line.strip()
        if "guardrail" in stripped.lower() or "non-negotiable" in stripped.lower():
            in_guardrails = True
            continue
        if in_guardrails and stripped.startswith("## "):
            break
        if in_guardrails and re.match(r"^\d+\.\s+", stripped):
            guardrails.append(re.sub(r"^\d+\.\s+", "", stripped).strip())
    return guardrails


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "unnamed"

"""Workspace vocabulary research.

Derives a per-workspace term vocabulary from the workspace's own evidence
instead of curated domain lists. Replaces the "claim", "medicare",
"encounter", etc. literal lookups that previously made inference
healthcare-RCM specific.

Evidence sources (in trust order):

1. `workspace_feature_definitions.json` — user-confirmed prior decisions
2. `data_dictionary.{md,csv,txt,json}` under docs/ — authoritative term map
3. `kpi_registry.json` + `kpi_feature_mapping.json` — KPI question/metric/cuts text
4. `profile_index.json` + per-dataset profiles — table names, column names,
   sample values, dtypes, cardinalities
5. `workspace_settings.json domain` hint (optional, coarse)

Output: `interns/generated/contracts/workspace_vocabulary.json` with
per-category term sets and per-term provenance. Universal generic seed
fallback is intentionally TINY — words common to every business domain.

Workspace-agnostic. Zero hardcoded domain vocabulary.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


VOCABULARY_VERSION = 1
LOW_CONFIDENCE_THRESHOLD = 0.4


GENERIC_FINANCIAL_SEED = (
    "amount", "cost", "price", "fee", "spend", "revenue", "income",
    "profit", "loss", "balance", "charge", "margin",
)
GENERIC_TEMPORAL_SEED = (
    "date", "time", "timestamp", "day", "week", "month", "quarter", "year",
)
GENERIC_IDENTIFIER_SUFFIXES = ("id", "code", "key", "uuid")


@dataclass
class TermEvidence:
    term: str
    sources: list[str] = field(default_factory=list)
    sample_values: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "sources": sorted(set(self.sources)),
            "sample_values": self.sample_values[:5],
            "confidence": round(self.confidence, 2),
        }


@dataclass
class VocabularyResult:
    workspace: str
    written_at: str
    categories: dict[str, list[TermEvidence]]
    confidence: float
    needs_user_confirmation: bool
    written_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "workspace_vocabulary.json",
            "version": VOCABULARY_VERSION,
            "workspace": self.workspace,
            "written_at": self.written_at,
            "overall_confidence": round(self.confidence, 2),
            "needs_user_confirmation": self.needs_user_confirmation,
            "categories": {
                name: [item.to_dict() for item in items]
                for name, items in self.categories.items()
            },
            "evidence_provenance_rule": (
                "Each term carries `sources` (file paths or contract sections) and "
                "`sample_values` (concrete evidence). Confidence is per-term + overall."
            ),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _columns_from_profile(profile: dict[str, Any]) -> list[tuple[str, str, list[Any]]]:
    """Return [(column_name, dtype, sample_values), ...] from a profile record."""
    out: list[tuple[str, str, list[Any]]] = []
    columns = profile.get("columns")
    if isinstance(columns, list):
        for col in columns:
            if not isinstance(col, dict):
                continue
            name = str(col.get("name") or col.get("column") or "")
            if not name:
                continue
            dtype = str(col.get("dtype") or "")
            samples = col.get("sample_values") or []
            out.append((name, dtype, list(samples) if isinstance(samples, list) else []))
        return out
    schema = profile.get("schema")
    if isinstance(schema, dict):
        for name, dtype in schema.items():
            out.append((str(name), str(dtype), []))
    return out


def _table_name_from_path(path: str) -> str:
    return Path(str(path)).stem


class WorkspaceResearcher:
    """Inspects a workspace and derives per-category vocabulary from its own evidence."""

    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self._results: dict[str, dict[str, TermEvidence]] = {}

    def research(self) -> VocabularyResult:
        self.layout.ensure_runtime_dirs()
        self._results = {
            "financial_terms": {},
            "temporal_terms": {},
            "entity_terms": {},
            "filter_terms": {},
            "identifier_terms": {},
        }

        self._mine_workspace_feature_definitions()
        self._mine_data_dictionary()
        self._mine_kpi_registry()
        self._mine_profiles()

        categories = {
            name: sorted(items.values(), key=lambda e: (-e.confidence, e.term))
            for name, items in self._results.items()
        }
        overall_confidence = self._overall_confidence(categories)
        needs_user_confirmation = overall_confidence < LOW_CONFIDENCE_THRESHOLD
        written_path = self._write(categories, overall_confidence, needs_user_confirmation)

        return VocabularyResult(
            workspace=self.workspace.name,
            written_at=_now(),
            categories=categories,
            confidence=overall_confidence,
            needs_user_confirmation=needs_user_confirmation,
            written_path=written_path,
        )

    def _add(
        self,
        category: str,
        term: str,
        *,
        source: str,
        confidence: float,
        sample: Any = None,
    ) -> None:
        if not term:
            return
        bucket = self._results.setdefault(category, {})
        normalized = _norm(term)
        if not normalized:
            return
        entry = bucket.get(normalized)
        if entry is None:
            entry = TermEvidence(term=normalized)
            bucket[normalized] = entry
        if source:
            entry.sources.append(source)
        if sample is not None and len(entry.sample_values) < 5:
            entry.sample_values.append(str(sample))
        entry.confidence = max(entry.confidence, confidence)

    def _mine_workspace_feature_definitions(self) -> None:
        path = self.layout.contracts_dir / "workspace_feature_definitions.json"
        data = _read_json(path)
        for definition in data.get("definitions") or data.get("entries") or []:
            if not isinstance(definition, dict):
                continue
            feature = str(definition.get("feature") or definition.get("name") or "")
            rule = str(definition.get("rule") or definition.get("definition") or "")
            if feature:
                category = self._classify_seed(feature, rule)
                self._add(category, feature, source=str(path), confidence=0.95)

    def _mine_data_dictionary(self) -> None:
        docs_dir = self.workspace / "docs"
        if not docs_dir.exists():
            return
        for path in sorted(docs_dir.rglob("*")):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            name_lower = path.name.lower()
            if not any(t in name_lower for t in ("dictionary", "model", "schema", "contract")):
                continue
            if suffix == ".csv":
                self._mine_dictionary_csv(path)
            elif suffix in {".md", ".txt"}:
                self._mine_dictionary_text(path)

    def _mine_dictionary_csv(self, path: Path) -> None:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    column = (row.get("column") or row.get("Column") or "").strip()
                    description = (row.get("description") or row.get("Description") or "").strip()
                    if not column:
                        continue
                    category = self._classify_seed(column, description)
                    self._add(category, column, source=str(path), confidence=0.9)
        except (OSError, csv.Error):
            return

    def _mine_dictionary_text(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        for match in re.finditer(r"`([A-Za-z][A-Za-z0-9_]+)`", text):
            term = match.group(1)
            category = self._classify_seed(term, text)
            self._add(category, term, source=str(path), confidence=0.6)

    def _mine_kpi_registry(self) -> None:
        registry = _read_json(self.layout.contracts_dir / "kpi_registry.json")
        for kpi in registry.get("kpis") or []:
            if not isinstance(kpi, dict):
                continue
            question = " ".join(
                str(kpi.get(field) or "") for field in ("name", "business_question", "description")
            )
            metric = str(kpi.get("metric") or "")
            cuts = str(kpi.get("cuts") or "")

            for match in re.finditer(
                r"['\"]([^'\"]{1,80})['\"]", " ".join([cuts, question, metric])
            ):
                literal = match.group(1).strip()
                if literal:
                    self._add("filter_terms", literal, source="kpi_registry.json::filter_literal",
                              confidence=0.85, sample=literal)
            for token in re.findall(r"\b[A-Za-z][A-Za-z0-9_]{2,}\b", metric):
                category = self._classify_seed(token, metric)
                if category in {"financial_terms", "temporal_terms"}:
                    self._add(category, token, source="kpi_registry.json::metric",
                              confidence=0.7, sample=metric)
            for token in re.findall(r"\b[A-Za-z][A-Za-z0-9_]{2,}\b", cuts):
                category = self._classify_seed(token, cuts)
                self._add(category, token, source="kpi_registry.json::cuts",
                          confidence=0.55, sample=cuts)

    def _mine_profiles(self) -> None:
        index = _read_json(self.layout.profile_index_path)
        for profile in index.get("profiles") or []:
            if not isinstance(profile, dict):
                continue
            path_str = str(profile.get("path") or "")
            if not path_str:
                continue
            table = _table_name_from_path(path_str)
            if table:
                normalized_table = _norm(table.rstrip("s") if table.endswith("s") else table)
                self._add(
                    "entity_terms",
                    normalized_table,
                    source=f"profile_index::{path_str}",
                    confidence=0.85,
                    sample=table,
                )
            for column_name, dtype, samples in _columns_from_profile(profile):
                category = self._classify_column(column_name, dtype, samples)
                self._add(
                    category,
                    column_name,
                    source=f"profile_index::{path_str}::{column_name}",
                    confidence=0.65,
                    sample=samples[0] if samples else None,
                )
                if dtype.lower() == "string" and 1 < len(samples) <= 10:
                    for value in samples:
                        text = str(value).strip()
                        if 1 < len(text) <= 40 and not re.search(r"\d{3,}", text):
                            self._add(
                                "filter_terms",
                                text,
                                source=f"profile_index::{path_str}::{column_name}::sample",
                                confidence=0.5,
                                sample=text,
                            )

    def _classify_seed(self, term: str, context: str = "") -> str:
        haystack = (term + " " + context).lower()
        if any(seed in haystack for seed in GENERIC_FINANCIAL_SEED):
            return "financial_terms"
        if any(seed in haystack for seed in GENERIC_TEMPORAL_SEED):
            return "temporal_terms"
        normalized = _norm(term)
        if any(normalized.endswith(suffix) for suffix in GENERIC_IDENTIFIER_SUFFIXES):
            return "identifier_terms"
        return "entity_terms"

    def _classify_column(self, column: str, dtype: str, samples: list[Any]) -> str:
        lowered_dtype = dtype.lower()
        if "date" in lowered_dtype or "time" in lowered_dtype:
            return "temporal_terms"
        if lowered_dtype in {"int64", "float64", "double", "decimal", "numeric"}:
            normalized = column.lower()
            if any(seed in normalized for seed in GENERIC_FINANCIAL_SEED):
                return "financial_terms"
        normalized = _norm(column)
        if any(normalized.endswith(suffix) for suffix in GENERIC_IDENTIFIER_SUFFIXES):
            return "identifier_terms"
        return self._classify_seed(column)

    def _overall_confidence(self, categories: dict[str, list[TermEvidence]]) -> float:
        total_terms = sum(len(items) for items in categories.values())
        if total_terms == 0:
            return 0.0
        weighted = sum(item.confidence for items in categories.values() for item in items)
        breadth_factor = min(1.0, total_terms / 15)
        return (weighted / total_terms) * breadth_factor

    def _write(
        self,
        categories: dict[str, list[TermEvidence]],
        confidence: float,
        needs_user_confirmation: bool,
    ) -> str:
        path = self.layout.contracts_dir / "workspace_vocabulary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "artifact_type": "workspace_vocabulary.json",
            "version": VOCABULARY_VERSION,
            "workspace": self.workspace.name,
            "written_at": _now(),
            "overall_confidence": round(confidence, 2),
            "needs_user_confirmation": needs_user_confirmation,
            "categories": {
                name: [item.to_dict() for item in items]
                for name, items in categories.items()
            },
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            return path.relative_to(self.repo_root).as_posix()
        except ValueError:
            return path.as_posix()


def research_workspace_vocabulary(
    repo_root: str | Path, workspace: str | Path
) -> VocabularyResult:
    return WorkspaceResearcher(repo_root, workspace).research()


__all__ = [
    "GENERIC_FINANCIAL_SEED",
    "GENERIC_TEMPORAL_SEED",
    "GENERIC_IDENTIFIER_SUFFIXES",
    "LOW_CONFIDENCE_THRESHOLD",
    "TermEvidence",
    "VocabularyResult",
    "WorkspaceResearcher",
    "research_workspace_vocabulary",
]

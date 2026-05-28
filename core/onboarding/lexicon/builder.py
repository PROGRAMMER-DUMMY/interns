"""Workspace lexicon builder.

The lexicon is a derived contract under
``workspaces/<project>/interns/generated/contracts/workspace_lexicon.json``.
It is built from existing workspace evidence and consumed by KPI inference
and schema-alias matching — replacing the previous hardcoded keyword
ladders. The builder does not ship any pre-baked vocabulary; an empty
workspace produces an empty lexicon, which is correct (do not confidently
infer from nothing).

Evidence sources, in confidence order:

1. ``workspace_feature_definitions.json`` — user-accepted definitions from
   prior blocker resolution. Highest confidence.
2. ``kpi_feature_mapping.json`` — confirmed term-to-column mappings from
   prior resolver runs.
3. ``kpi_registry.json`` — KPI rows whose ``metric`` or ``cuts`` cells were
   authored. Phrases extracted from each row's ``name`` are paired with the
   authored values.
4. ``profile_index.json`` — every column from every profile contributes its
   canonical name plus normalized forms.

Lexicon shape (the on-disk contract):

```jsonc
{
  "artifact_type": "workspace_lexicon.json",
  "version": 1,
  "workspace": "workspaces/<project>",
  "generated_at": "<iso8601>",
  "metric_phrases": [
    {"phrase": "amount paid", "metric": "PaidAmount",
     "source": "kpi_registry", "kpi_ids": ["kpi_001"], "confidence": "authored"}
  ],
  "cut_phrases": [
    {"phrase": "by payer", "cut": "Payer",
     "source": "kpi_registry", "kpi_ids": ["kpi_001"], "confidence": "authored"}
  ],
  "column_aliases": {
    "PaidAmount": {
      "normalized": ["paidamount"],
      "from_user_definitions": ["paid", "amount paid"],
      "from_dictionary": [],
      "sources": ["profile:claims.csv"]
    }
  },
  "cuts_headers": ["cuts", "cuts with drg consolidated"],
  "stats": { "...": "..." }
}
```
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.storage.workspace_layout import WorkspaceLayout
from core.contracts.versioning import register_contract

LOGGER = logging.getLogger(__name__)

ARTIFACT_TYPE = "workspace_lexicon.json"
ARTIFACT_VERSION = 1

register_contract(ARTIFACT_TYPE, current_version=ARTIFACT_VERSION)
LEXICON_FILENAME = "workspace_lexicon.json"

_STOPWORDS = frozenset({
    "the", "a", "an", "of", "by", "in", "on", "at", "to", "for", "and", "or",
    "with", "from", "is", "are", "do", "does", "what", "which", "how", "this",
    "that", "per", "vs", "versus",
})
_MIN_PHRASE_TOKENS = 1
_MAX_PHRASE_TOKENS = 4


@dataclass(frozen=True)
class MetricPhrase:
    phrase: str
    metric: str
    source: str
    kpi_ids: tuple[str, ...] = ()
    confidence: str = "authored"

    def to_dict(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "metric": self.metric,
            "source": self.source,
            "kpi_ids": list(self.kpi_ids),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class CutPhrase:
    phrase: str
    cut: str
    source: str
    kpi_ids: tuple[str, ...] = ()
    confidence: str = "authored"

    def to_dict(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "cut": self.cut,
            "source": self.source,
            "kpi_ids": list(self.kpi_ids),
            "confidence": self.confidence,
        }


@dataclass
class ColumnAliasEntry:
    canonical: str
    normalized: set[str] = field(default_factory=set)
    from_user_definitions: set[str] = field(default_factory=set)
    from_dictionary: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalized": sorted(self.normalized),
            "from_user_definitions": sorted(self.from_user_definitions),
            "from_dictionary": sorted(self.from_dictionary),
            "sources": sorted(self.sources),
        }

    def all_aliases(self) -> set[str]:
        return set().union(
            self.normalized,
            self.from_user_definitions,
            self.from_dictionary,
        )


class WorkspaceLexicon:
    """In-memory matcher backed by a workspace_lexicon.json payload."""

    def __init__(
        self,
        *,
        metric_phrases: list[MetricPhrase],
        cut_phrases: list[CutPhrase],
        column_aliases: dict[str, ColumnAliasEntry],
        cuts_headers: list[str],
        workspace: str = "",
        generated_at: str = "",
        stats: dict[str, Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.generated_at = generated_at
        self.metric_phrases = sorted(
            metric_phrases, key=lambda p: (-len(p.phrase), p.phrase)
        )
        self.cut_phrases = sorted(
            cut_phrases, key=lambda p: (-len(p.phrase), p.phrase)
        )
        self.column_aliases = column_aliases
        self.cuts_headers = list(cuts_headers)
        self.stats = stats or {}

    def is_empty(self) -> bool:
        return not (self.metric_phrases or self.cut_phrases or self.column_aliases)

    def infer_metric_and_cuts(self, name: str, description: str = "") -> tuple[str, str]:
        text = _normalize_text(f"{name} {description}")
        if not text:
            return "", ""
        metric = ""
        for entry in self.metric_phrases:
            if entry.phrase and entry.phrase in text:
                metric = entry.metric
                break
        cuts: list[str] = []
        for entry in self.cut_phrases:
            if entry.phrase and entry.phrase in text and entry.cut not in cuts:
                cuts.append(entry.cut)
        return metric, ", ".join(cuts)

    def aliases_for_column(self, column: str) -> set[str]:
        normalized = _normalize_token(column)
        entry = self.column_aliases.get(column) or self.column_aliases.get(normalized)
        if entry:
            return entry.all_aliases() | {normalized}
        return {normalized} if normalized else set()

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": ARTIFACT_TYPE,
            "version": ARTIFACT_VERSION,
            "generated_by": "lexicon-builder",
            "workspace": self.workspace,
            "generated_at": self.generated_at,
            "metric_phrases": [p.to_dict() for p in self.metric_phrases],
            "cut_phrases": [p.to_dict() for p in self.cut_phrases],
            "column_aliases": {
                column: entry.to_dict()
                for column, entry in sorted(self.column_aliases.items())
            },
            "cuts_headers": list(self.cuts_headers),
            "stats": self.stats,
        }


def build_workspace_lexicon(
    layout: WorkspaceLayout,
    repo_root: str | Path,
) -> WorkspaceLexicon:
    """Derive a lexicon from the workspace's existing generated artifacts.

    Reads `kpi_registry.json`, `profile_index.json`,
    `workspace_feature_definitions.json`, and `kpi_feature_mapping.json` when
    present. Missing artifacts contribute nothing — they are not an error.
    Writes the result to `interns/generated/contracts/workspace_lexicon.json`
    and returns the in-memory matcher.
    """
    repo_root = Path(repo_root).resolve()
    contracts_dir = layout.contracts_dir
    kpi_registry = _read_json(contracts_dir / "kpi_registry.json")
    feature_mapping = _read_json(contracts_dir / "kpi_feature_mapping.json")
    feature_definitions = _read_json(contracts_dir / "workspace_feature_definitions.json")
    profile_index = _read_json(layout.profiles_dir / "profile_index.json")

    metric_phrases: list[MetricPhrase] = []
    cut_phrases: list[CutPhrase] = []
    column_aliases: dict[str, ColumnAliasEntry] = {}
    observed_headers: list[str] = []
    stats: dict[str, Any] = {
        "kpi_rows_examined": 0,
        "kpi_rows_with_authored_metric": 0,
        "kpi_rows_with_authored_cuts": 0,
        "columns_indexed": 0,
        "accepted_definitions_carried": 0,
        "mappings_carried": 0,
    }

    _harvest_from_kpi_registry(
        kpi_registry,
        metric_phrases=metric_phrases,
        cut_phrases=cut_phrases,
        observed_headers=observed_headers,
        stats=stats,
    )
    _harvest_from_profile_index(
        profile_index,
        column_aliases=column_aliases,
        stats=stats,
    )
    _harvest_from_feature_definitions(
        feature_definitions,
        column_aliases=column_aliases,
        metric_phrases=metric_phrases,
        stats=stats,
    )
    _harvest_from_feature_mapping(
        feature_mapping,
        column_aliases=column_aliases,
        stats=stats,
    )

    workspace_rel = _rel(layout.project_root, repo_root)
    lexicon = WorkspaceLexicon(
        metric_phrases=_dedupe_metric_phrases(metric_phrases),
        cut_phrases=_dedupe_cut_phrases(cut_phrases),
        column_aliases=column_aliases,
        cuts_headers=_dedupe_lower_preserve_case(observed_headers),
        workspace=workspace_rel,
        generated_at=datetime.now(timezone.utc).isoformat(),
        stats=stats,
    )

    layout.contracts_dir.mkdir(parents=True, exist_ok=True)
    (layout.contracts_dir / LEXICON_FILENAME).write_text(
        json.dumps(lexicon.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )
    return lexicon


def load_workspace_lexicon(layout: WorkspaceLayout) -> WorkspaceLexicon | None:
    """Read a previously built lexicon. Returns None when absent or unreadable."""
    path = layout.contracts_dir / LEXICON_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("workspace_lexicon_unreadable:%s:%s", path, exc)
        return None
    return _lexicon_from_payload(payload)


def _lexicon_from_payload(payload: dict[str, Any]) -> WorkspaceLexicon:
    metric_phrases = [
        MetricPhrase(
            phrase=str(entry.get("phrase") or ""),
            metric=str(entry.get("metric") or ""),
            source=str(entry.get("source") or ""),
            kpi_ids=tuple(entry.get("kpi_ids") or ()),
            confidence=str(entry.get("confidence") or "derived"),
        )
        for entry in payload.get("metric_phrases") or []
        if isinstance(entry, dict)
    ]
    cut_phrases = [
        CutPhrase(
            phrase=str(entry.get("phrase") or ""),
            cut=str(entry.get("cut") or ""),
            source=str(entry.get("source") or ""),
            kpi_ids=tuple(entry.get("kpi_ids") or ()),
            confidence=str(entry.get("confidence") or "derived"),
        )
        for entry in payload.get("cut_phrases") or []
        if isinstance(entry, dict)
    ]
    column_aliases: dict[str, ColumnAliasEntry] = {}
    for column, entry in (payload.get("column_aliases") or {}).items():
        if not isinstance(entry, dict):
            continue
        column_aliases[column] = ColumnAliasEntry(
            canonical=column,
            normalized=set(entry.get("normalized") or []),
            from_user_definitions=set(entry.get("from_user_definitions") or []),
            from_dictionary=set(entry.get("from_dictionary") or []),
            sources=set(entry.get("sources") or []),
        )
    return WorkspaceLexicon(
        metric_phrases=metric_phrases,
        cut_phrases=cut_phrases,
        column_aliases=column_aliases,
        cuts_headers=list(payload.get("cuts_headers") or []),
        workspace=str(payload.get("workspace") or ""),
        generated_at=str(payload.get("generated_at") or ""),
        stats=payload.get("stats") or {},
    )


def _harvest_from_kpi_registry(
    payload: dict[str, Any] | None,
    *,
    metric_phrases: list[MetricPhrase],
    cut_phrases: list[CutPhrase],
    observed_headers: list[str],
    stats: dict[str, Any],
) -> None:
    if not isinstance(payload, dict):
        return
    rows = payload.get("kpis") or payload.get("rows") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stats["kpi_rows_examined"] += 1
        name = str(row.get("name") or row.get("kpi") or row.get("question") or "")
        if not name.strip():
            continue
        kpi_id = str(row.get("id") or row.get("kpi_id") or "")
        metric_cell = str(row.get("metric") or "").strip()
        cuts_cell = str(row.get("cuts") or row.get("grain") or row.get("dimensions") or "").strip()
        if metric_cell:
            stats["kpi_rows_with_authored_metric"] += 1
            for phrase in _phrases_from_name(name):
                metric_phrases.append(
                    MetricPhrase(
                        phrase=phrase,
                        metric=metric_cell,
                        source="kpi_registry",
                        kpi_ids=(kpi_id,) if kpi_id else (),
                        confidence="authored",
                    )
                )
        if cuts_cell:
            stats["kpi_rows_with_authored_cuts"] += 1
            for cut_value in _split_cuts(cuts_cell):
                for phrase in _phrases_from_name(name):
                    cut_phrases.append(
                        CutPhrase(
                            phrase=phrase,
                            cut=cut_value,
                            source="kpi_registry",
                            kpi_ids=(kpi_id,) if kpi_id else (),
                            confidence="authored",
                        )
                    )
    headers = payload.get("observed_headers") or payload.get("registry_headers") or []
    for header in headers:
        if isinstance(header, str) and header.strip():
            observed_headers.append(header)


def _harvest_from_profile_index(
    payload: dict[str, Any] | None,
    *,
    column_aliases: dict[str, ColumnAliasEntry],
    stats: dict[str, Any],
) -> None:
    if not isinstance(payload, dict):
        return
    profiles = payload.get("profiles") or []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        dataset = str(profile.get("path") or "")
        schema = profile.get("schema") or {}
        if not isinstance(schema, dict):
            continue
        for column_name in schema.keys():
            if not isinstance(column_name, str) or not column_name.strip():
                continue
            entry = column_aliases.setdefault(
                column_name, ColumnAliasEntry(canonical=column_name)
            )
            entry.normalized.add(_normalize_token(column_name))
            entry.sources.add(f"profile:{dataset}" if dataset else "profile")
            stats["columns_indexed"] += 1


def _harvest_from_feature_definitions(
    payload: dict[str, Any] | None,
    *,
    column_aliases: dict[str, ColumnAliasEntry],
    metric_phrases: list[MetricPhrase],
    stats: dict[str, Any],
) -> None:
    if not isinstance(payload, dict):
        return
    definitions = payload.get("definitions") or payload.get("workspace_definitions") or {}
    if not isinstance(definitions, dict):
        return
    for feature_term, definition in definitions.items():
        if not isinstance(definition, dict):
            continue
        stats["accepted_definitions_carried"] += 1
        # source_columns in the on-disk artifact is a list of either bare column
        # name strings or column-spec dicts ({"column": "...", "dataset": "..."}).
        # Accept both shapes for robustness.
        for column_spec in definition.get("source_columns") or []:
            if isinstance(column_spec, str):
                column = column_spec
            elif isinstance(column_spec, dict):
                column = str(column_spec.get("column") or "").strip()
            else:
                continue
            if not column:
                continue
            entry = column_aliases.setdefault(
                column, ColumnAliasEntry(canonical=column)
            )
            entry.from_user_definitions.add(_normalize_token(feature_term))
            entry.sources.add("workspace_feature_definitions")
        target = definition.get("metric") or definition.get("expression")
        if isinstance(target, str) and target.strip():
            metric_phrases.append(
                MetricPhrase(
                    phrase=_normalize_text(feature_term),
                    metric=target.strip(),
                    source="workspace_feature_definitions",
                    confidence="user_confirmed",
                )
            )


def _harvest_from_feature_mapping(
    payload: dict[str, Any] | None,
    *,
    column_aliases: dict[str, ColumnAliasEntry],
    stats: dict[str, Any],
) -> None:
    if not isinstance(payload, dict):
        return
    kpis = payload.get("kpis") or []
    if not isinstance(kpis, list):
        return
    ready_states = {"ready", "user_confirmed", "proven_data_model"}
    for kpi in kpis:
        if not isinstance(kpi, dict):
            continue
        features = kpi.get("features") or []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            state = str(feature.get("state") or "").lower()
            if state not in ready_states:
                continue
            feature_term = str(feature.get("feature") or "").strip()
            if not feature_term:
                continue
            for column_info in feature.get("source_columns") or []:
                if not isinstance(column_info, dict):
                    continue
                column = str(column_info.get("column") or "").strip()
                if not column:
                    continue
                entry = column_aliases.setdefault(
                    column, ColumnAliasEntry(canonical=column)
                )
                entry.from_user_definitions.add(_normalize_token(feature_term))
                entry.sources.add("kpi_feature_mapping")
                stats["mappings_carried"] += 1


def _phrases_from_name(name: str) -> list[str]:
    tokens = [
        token for token in re.split(r"[^a-z0-9]+", name.lower()) if token
    ]
    tokens = [token for token in tokens if token not in _STOPWORDS]
    phrases: list[str] = []
    for size in range(_MIN_PHRASE_TOKENS, _MAX_PHRASE_TOKENS + 1):
        for start in range(0, len(tokens) - size + 1):
            phrase = " ".join(tokens[start : start + size])
            if phrase and phrase not in phrases:
                phrases.append(phrase)
    return phrases


def _split_cuts(value: str) -> list[str]:
    parts = re.split(r"[,;\n]+", value)
    return [part.strip() for part in parts if part.strip()]


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("lexicon_source_unreadable:%s:%s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _rel(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _dedupe_metric_phrases(phrases: Iterable[MetricPhrase]) -> list[MetricPhrase]:
    seen: dict[tuple[str, str], MetricPhrase] = {}
    for phrase in phrases:
        if not phrase.phrase or not phrase.metric:
            continue
        key = (phrase.phrase, phrase.metric)
        existing = seen.get(key)
        if existing is None:
            seen[key] = phrase
        else:
            merged_ids = tuple(dict.fromkeys((*existing.kpi_ids, *phrase.kpi_ids)))
            seen[key] = MetricPhrase(
                phrase=phrase.phrase,
                metric=phrase.metric,
                source=existing.source or phrase.source,
                kpi_ids=merged_ids,
                confidence=_higher_confidence(existing.confidence, phrase.confidence),
            )
    return list(seen.values())


def _dedupe_cut_phrases(phrases: Iterable[CutPhrase]) -> list[CutPhrase]:
    seen: dict[tuple[str, str], CutPhrase] = {}
    for phrase in phrases:
        if not phrase.phrase or not phrase.cut:
            continue
        key = (phrase.phrase, phrase.cut)
        existing = seen.get(key)
        if existing is None:
            seen[key] = phrase
        else:
            merged_ids = tuple(dict.fromkeys((*existing.kpi_ids, *phrase.kpi_ids)))
            seen[key] = CutPhrase(
                phrase=phrase.phrase,
                cut=phrase.cut,
                source=existing.source or phrase.source,
                kpi_ids=merged_ids,
                confidence=_higher_confidence(existing.confidence, phrase.confidence),
            )
    return list(seen.values())


def _higher_confidence(a: str, b: str) -> str:
    order = {"user_confirmed": 3, "authored": 2, "derived": 1, "": 0}
    return a if order.get(a, 0) >= order.get(b, 0) else b


def _dedupe_lower_preserve_case(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out

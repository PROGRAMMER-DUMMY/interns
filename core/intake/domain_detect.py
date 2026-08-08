"""Which business domain is this workspace's data from -- DERIVED, never assumed.

The intake interview cannot ask good questions without knowing what kind of
business it is talking to, and it must not decide that by itself. So this module
does the cheap, attackable half: it matches a vocabulary of characteristic entity
tokens (data, in ``domain_vocabulary.json``) against evidence the workspace
already produced -- discovered table names, discovered column names, document
filenames, KPI registry terms -- and reports what matched. The user confirms or
overrides it in the first intake question.

Two rules, the same ones the discovery scanner follows:

1. **No invented domain.** Nothing matched means ``unknown`` at confidence 0.0.
   A guessed domain would silently retune every later default, which is the same
   failure mode as a fabricated byte size.
2. **Every inference carries its evidence.** :attr:`DomainSignal.evidence` names
   the exact token and the exact table/column/document it came from, so an
   operator can attack the inference instead of trusting it.

Follows ``core/onboarding/lexicon/vocabulary.py``: the words live in data, the
code only matches them. Adding or retuning a domain is a data edit.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

VOCABULARY_PATH = Path(__file__).with_name("domain_vocabulary.json")

UNKNOWN_DOMAIN = "unknown"

# ponytail: two documented constants, not a model. `_FULL_CONFIDENCE_TOKENS`
# distinct matches with no rival domain is the most this heuristic will ever
# claim; one lonely match caps out at a third of that. Swap in a real classifier
# only if a real estate proves this wrong -- the evidence list is printed either
# way, so a reviewer can disagree with the number.
_FULL_CONFIDENCE_TOKENS = 3
_MAX_EVIDENCE_LINES = 12

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class DomainSignal:
    """What the evidence says, and what it rested on."""

    domain: str
    confidence: float
    evidence: list[str]
    alternatives: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_known(self) -> bool:
        return self.domain != UNKNOWN_DOMAIN


@lru_cache(maxsize=1)
def load_vocabulary() -> dict[str, dict[str, Any]]:
    """``{slug: {label, tokens, question_overlay}}`` from the data file."""
    try:
        data = json.loads(VOCABULARY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    domains = data.get("domains") if isinstance(data, dict) else None
    return {
        str(slug): value
        for slug, value in (domains or {}).items()
        if isinstance(value, dict)
    }


def domain_labels() -> dict[str, str]:
    """``{slug: human label}`` for every domain the vocabulary knows."""
    return {slug: str(entry.get("label") or slug) for slug, entry in load_vocabulary().items()}


def overlay_for(domain: str) -> dict[str, dict[str, Any]]:
    """The per-question overlay a CONFIRMED domain carries, or ``{}``.

    Keyed by intake question id. Never introduces or renames an option id -- the
    caller applies labels and recommendations onto the ids that already exist.
    """
    entry = load_vocabulary().get(str(domain or ""), {})
    overlay = entry.get("question_overlay")
    return {str(k): v for k, v in (overlay or {}).items() if isinstance(v, dict)}


def detect_domain(
    discovery: dict[str, Any] | None,
    documents: list[str] | None = None,
    kpi_terms: list[str] | None = None,
) -> DomainSignal:
    """Match the vocabulary against workspace evidence.

    `discovery` is the ``intake/discovery.json`` payload (``{}`` or ``None`` when
    the scan has not run -- that is not an error, it just means less evidence);
    `documents` are document filenames or paths; `kpi_terms` are strings lifted
    from the workspace KPI registry.
    """
    vocabulary = load_vocabulary()
    items = list(_evidence_items(discovery or {}, documents, kpi_terms))

    # {domain: {token: "kind `value`"}} -- one line per distinct token, so ten
    # part-files of the same table cannot inflate a score.
    hits: dict[str, dict[str, str]] = {slug: {} for slug in vocabulary}
    for kind, value in items:
        terms = _terms(value)
        if not terms:
            continue
        for slug, entry in vocabulary.items():
            for token in entry.get("tokens") or []:
                token = str(token).lower()
                if token in terms:
                    hits[slug].setdefault(token, f"{kind} `{value}` matched `{token}`")

    ranked = sorted(
        ((slug, len(tokens)) for slug, tokens in hits.items() if tokens),
        key=lambda pair: (-pair[1], pair[0]),
    )
    if not ranked:
        return DomainSignal(domain=UNKNOWN_DOMAIN, confidence=0.0, evidence=[], alternatives=[])

    top_slug, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    separation = top_score / (top_score + runner_up)
    strength = min(1.0, top_score / _FULL_CONFIDENCE_TOKENS)
    return DomainSignal(
        domain=top_slug,
        confidence=round(separation * strength, 2),
        evidence=sorted(hits[top_slug].values())[:_MAX_EVIDENCE_LINES],
        alternatives=[slug for slug, _ in ranked[1:]],
    )


def _evidence_items(
    discovery: dict[str, Any],
    documents: list[str] | None,
    kpi_terms: list[str] | None,
) -> Iterable[tuple[str, str]]:
    """``(kind, value)`` in evidence order: tables, columns, documents, KPI terms."""
    tables = [table for table in (discovery.get("tables") or []) if isinstance(table, dict)]
    for table in tables:
        name = str(table.get("name") or "").strip()
        if name:
            yield "table", name
    for table in tables:
        for column in table.get("columns") or []:
            name = str(column.get("name") if isinstance(column, dict) else column or "").strip()
            if name:
                yield "column", name
    for document in documents or []:
        name = Path(str(document)).name.strip()
        if name:
            yield "document", name
    for term in kpi_terms or []:
        text = str(term).strip()
        if text:
            yield "kpi term", text


def _terms(text: str) -> set[str]:
    """Lowercase words in `text`, split on camelCase and punctuation, depluralised.

    ``ClaimLines`` and ``claim_line`` both reduce to a set containing ``claim``.
    """
    words = _WORD.findall(_CAMEL_BOUNDARY.sub(" ", str(text)).lower())
    terms: set[str] = set()
    for word in words:
        terms.add(word)
        if len(word) > 4 and word.endswith("ies"):
            terms.add(word[:-3] + "y")
        if len(word) > 4 and word.endswith("es"):
            terms.add(word[:-2])
        if len(word) > 3 and word.endswith("s"):
            terms.add(word[:-1])
    return terms


__all__ = [
    "UNKNOWN_DOMAIN",
    "VOCABULARY_PATH",
    "DomainSignal",
    "detect_domain",
    "domain_labels",
    "load_vocabulary",
    "overlay_for",
]

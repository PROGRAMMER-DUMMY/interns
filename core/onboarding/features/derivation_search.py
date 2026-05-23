"""Search reusable derivation patterns without treating them as proof."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PATTERNS_PATH = Path(__file__).with_name("derivation_patterns.json")


@dataclass(frozen=True)
class DerivationSearchInput:
    feature: str
    available_columns: list[str] = field(default_factory=list)
    expression_context: str = ""
    domain: str = "general"
    prior_decisions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DerivationCandidate:
    pattern_id: str
    score: float
    state: str
    description: str
    required_inputs: list[str]
    candidate_bindings: dict[str, list[str]]
    requires: list[str]
    templates: dict[str, str]
    clarification_prompt: str
    evidence_gap: str = "pattern_is_reusable_not_workspace_proof"

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "score": self.score,
            "state": self.state,
            "description": self.description,
            "required_inputs": self.required_inputs,
            "candidate_bindings": self.candidate_bindings,
            "requires": self.requires,
            "templates": self.templates,
            "clarification_prompt": self.clarification_prompt,
            "evidence_gap": self.evidence_gap,
        }


class DerivationPatternSearcher:
    def __init__(self, patterns_path: str | Path = PATTERNS_PATH):
        self.patterns_path = Path(patterns_path)
        self.patterns = self._load_patterns()

    def search(self, request: DerivationSearchInput, limit: int = 5) -> list[DerivationCandidate]:
        candidates = []
        feature_norm = _norm(request.feature)
        context_norm = _norm(request.expression_context)
        available = list(dict.fromkeys(request.available_columns))
        available_norm = {_norm(column): column for column in available}
        for pattern in self.patterns:
            terms = [_norm(term) for term in pattern.get("feature_terms", [])]
            domains = pattern.get("domain", ["general"])
            score = 0.0
            feature_matched = False
            if feature_norm in terms:
                score += 10.0
                feature_matched = True
            elif any(_safe_partial_feature_match(feature_norm, term) for term in terms):
                score += 6.0
                feature_matched = True
            if not feature_matched:
                continue
            if request.domain in domains or "general" in domains:
                score += 1.5
            if context_norm and any(term in context_norm for term in terms):
                score += 2.0
            bindings = _candidate_bindings(pattern, available_norm)
            score += min(sum(len(values) for values in bindings.values()), 4)
            if score <= 0:
                continue
            candidates.append(
                DerivationCandidate(
                    pattern_id=pattern["id"],
                    score=round(score, 4),
                    state="candidate_pattern",
                    description=pattern.get("description", ""),
                    required_inputs=list(pattern.get("required_inputs", [])),
                    candidate_bindings=bindings,
                    requires=list(pattern.get("requires", [])),
                    templates=dict(pattern.get("templates", {})),
                    clarification_prompt=pattern.get("clarification_prompt", ""),
                )
            )
        candidates.sort(key=lambda item: (-item.score, item.pattern_id))
        return candidates[:limit]

    def _load_patterns(self) -> list[dict[str, Any]]:
        data = json.loads(self.patterns_path.read_text(encoding="utf-8"))
        return list(data.get("patterns", []))


def _candidate_bindings(pattern: dict[str, Any], available_norm: dict[str, str]) -> dict[str, list[str]]:
    bindings: dict[str, list[str]] = {}
    for input_name, candidate_columns in pattern.get("candidate_columns", {}).items():
        matched = []
        for candidate in candidate_columns:
            normalized = _norm(candidate)
            if normalized in available_norm:
                matched.append(available_norm[normalized])
        if matched:
            bindings[input_name] = matched
    return bindings


def _safe_partial_feature_match(feature_norm: str, term_norm: str) -> bool:
    if not feature_norm or not term_norm:
        return False
    if feature_norm in term_norm:
        return len(feature_norm) >= 5
    if term_norm in feature_norm:
        return len(term_norm) >= 5
    return False


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())

"""Workspace-agnostic KPI file format detector.

Identifies, with evidence and confidence, what role each column of a KPI
registry/tracker plays. Resolution is two-stage and value-driven, not positional:

  1. HEADER synonym match (exact / contained / fuzzy) against the canonical KPI
     roles.
  2. CONTENT signature when the header is missing or ambiguous: the column's own
     values decide the role (a column dominated by ``sum(/count(/ratio`` reads as
     a metric; short interrogative strings as the business question; comma-listed
     short tokens as cuts; comparison operators as filters).

This is what lets a 3-column file and a 12-column file both be understood, and it
is what feeds the confirmation panel's per-column "here is my understanding"
read-back. The detector NEVER auto-commits: it returns confidence + evidence so a
low/medium-confidence mapping can be confirmed by the user before use.

Zero domain vocabulary: header synonyms are generic metric-sheet terms and the
content signatures key off SQL-aggregate tokens and punctuation, never the
business domain. Pure and deterministic (no I/O) so it is trivially testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from core.onboarding.kpi.text_parser import KPI_CUTS_HEADERS

# Canonical KPI roles, in the order a read-back should present them. This is the
# same decomposition the kpi-analyst / kpi-clarification skills enumerate.
CANONICAL_ROLES: tuple[str, ...] = (
    "business_question",
    "metric",
    "cuts",
    "filters",
    "description",
    "target",
    "refinement",
)

# Roles a usable KPI registry must resolve; absence lowers overall confidence and
# is surfaced for confirmation rather than silently tolerated.
REQUIRED_ROLES: tuple[str, ...] = ("business_question", "metric")

ROLE_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "business_question": (
        "key business question", "business question", "kpi", "kpi name",
        "name", "question", "indicator", "measure",
    ),
    "metric": ("metric", "formula", "expression", "calculation", "computation"),
    "cuts": tuple(KPI_CUTS_HEADERS) + ("dimension", "dimensions", "slice", "breakdown", "group by", "segment"),
    "filters": ("filter", "filters", "condition", "conditions", "where", "scope", "inclusion", "exclusion"),
    "description": ("description", "definition", "notes", "note", "comment", "comments", "detail"),
    "target": ("target", "goal", "threshold", "benchmark", "objective"),
    "refinement": (
        "data model refinement required", "refinement required", "open questions",
        "open question", "clarification", "to clarify",
    ),
}

_HIGH = 0.85
_MED = 0.60

_AGG_TOKEN = re.compile(
    r"\b(sum|count|avg|average|mean|min|max|median|ratio|percent(?:age)?|distinct)\b\s*\(?",
    re.IGNORECASE,
)
_INTERROGATIVE = re.compile(
    r"^\s*(what|how|which|why|when|who|number of|count of|share of|percentage of)\b",
    re.IGNORECASE,
)
_COMPARISON = re.compile(r"[=<>]|(?:\bonly\b|\bexclud|\bwhere\b|\bstatus\b|\bin\s*\()", re.IGNORECASE)
_DIMENSION_LIST = re.compile(r"^[A-Za-z0-9 _]+(?:\s*[,;]\s*[A-Za-z0-9 _]+)+$")
_HIER_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)+\b")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _label(confidence: float) -> str:
    if confidence >= _HIGH:
        return "HIGH"
    if confidence >= _MED:
        return "MED"
    return "LOW"


@dataclass(frozen=True)
class ColumnRole:
    """One column's inferred role, with the evidence that justifies it."""

    header: str
    column_index: int
    role: str | None
    confidence: float
    confidence_label: str
    evidence: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "header": self.header,
            "column_index": self.column_index,
            "role": self.role,
            "confidence": round(self.confidence, 3),
            "confidence_label": self.confidence_label,
            "evidence": list(self.evidence),
            "reason": self.reason,
            "needs_user_confirmation": self.role is not None and self.confidence < _HIGH,
        }


@dataclass
class KpiFormatDetection:
    """Structured result of detecting a KPI file's format."""

    source: str
    columns: list[ColumnRole]
    row_count: int
    overall_confidence: float
    overall_label: str
    unmapped_headers: list[str] = field(default_factory=list)
    missing_required_roles: list[str] = field(default_factory=list)
    nesting_suspected: bool = False
    nesting_signals: list[str] = field(default_factory=list)

    def role_header(self, role: str) -> str | None:
        for col in self.columns:
            if col.role == role:
                return col.header
        return None

    def read_back(self, row: dict[str, Any]) -> dict[str, str]:
        """Map one raw row to {role: value} in canonical order.

        This is the consequence-of-mapping the confirmation panel renders so the
        user verifies meaning, not just the column-to-role table.
        """
        out: dict[str, str] = {}
        for role in CANONICAL_ROLES:
            header = self.role_header(role)
            if header is None:
                continue
            value = row.get(header)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                out[role] = text
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "row_count": self.row_count,
            "overall_confidence": round(self.overall_confidence, 3),
            "overall_label": self.overall_label,
            "columns": [c.to_dict() for c in self.columns],
            "unmapped_headers": list(self.unmapped_headers),
            "missing_required_roles": list(self.missing_required_roles),
            "nesting_suspected": self.nesting_suspected,
            "nesting_signals": list(self.nesting_signals),
            "needs_user_confirmation": self.overall_label != "HIGH"
            or bool(self.missing_required_roles)
            or self.nesting_suspected,
        }


def _header_score(header: str, role: str) -> float:
    norm = _norm(header)
    if not norm:
        return 0.0
    best = 0.0
    for syn in ROLE_HEADER_SYNONYMS[role]:
        syn_n = _norm(syn)
        if norm == syn_n:
            return 1.0
        if syn_n and (syn_n in norm or norm in syn_n):
            best = max(best, 0.85)
        else:
            best = max(best, SequenceMatcher(None, norm, syn_n).ratio() * 0.8)
    return best


def _content_score(values: list[str], role: str) -> float:
    """Fraction of non-empty cells whose VALUE signals the given role."""
    cells = [v for v in values if v]
    if not cells:
        return 0.0
    n = len(cells)
    if role == "metric":
        hits = sum(1 for v in cells if _AGG_TOKEN.search(v) or "/" in v)
        return hits / n
    if role == "business_question":
        hits = sum(1 for v in cells if v.rstrip().endswith("?") or _INTERROGATIVE.match(v))
        return hits / n
    if role == "cuts":
        def _dimension_like(v: str) -> bool:
            if _AGG_TOKEN.search(v) or v.rstrip().endswith("?"):
                return False
            if _DIMENSION_LIST.match(v):  # comma/semicolon list of tokens
                return True
            # A single short dimension token (must start with a letter, so pure
            # numbers fall to `target`, not `cuts`).
            return len(v.split()) <= 3 and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9 _]*", v))
        hits = sum(1 for v in cells if _dimension_like(v))
        return hits / n
    if role == "filters":
        hits = sum(1 for v in cells if _COMPARISON.search(v))
        return hits / n
    if role == "target":
        hits = sum(1 for v in cells if re.fullmatch(r"[<>]?=?\s*\d[\d,. %]*", v.strip()))
        return hits / n
    return 0.0


def _evidence(values: list[str], limit: int = 3) -> list[str]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v if len(v) <= 60 else v[:57] + "...")
        if len(out) >= limit:
            break
    return out


def _detect_nesting(
    rows: list[dict[str, Any]],
    name_header: str | None,
    *,
    name_col_index: int | None = None,
    merged_spans: list[tuple[int, int, int]] | None = None,
) -> tuple[bool, list[str]]:
    signals: list[str] = []
    if not rows or name_header is None:
        return False, signals
    names = [str(r.get(name_header) or "").strip() for r in rows]
    populated_seen = False
    blank_under_populated = 0
    for name in names:
        if name:
            populated_seen = True
        elif populated_seen:
            blank_under_populated += 1
    if blank_under_populated:
        signals.append(
            f"{blank_under_populated} blank-key row(s) under a populated KPI "
            "(possible sub-KPIs inheriting the parent)"
        )
    hier = sum(1 for name in names if _HIER_NUMBER.match(name))
    if hier:
        signals.append(f"{hier} row(s) use hierarchical numbering (e.g. 1.1, 1.2)")
    # Merged-cell parents in the key column: a label cell that spans several data
    # rows is the strongest structural nesting signal (a parent KPI whose name
    # visually brackets its sub-KPI rows).
    if merged_spans and name_col_index is not None:
        merged_parents = sum(
            1 for (col, start, end) in merged_spans
            if col == name_col_index and end > start
        )
        if merged_parents:
            signals.append(
                f"{merged_parents} merged-cell label(s) in the key column span multiple "
                "rows (parent KPI bracketing sub-KPIs)"
            )
    return bool(signals), signals


def detect_kpi_format(
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    source: str = "",
    merged_spans: list[tuple[int, int, int]] | None = None,
) -> KpiFormatDetection:
    """Detect each column's KPI role from headers and values, with confidence.

    ``columns`` are the raw header strings; ``rows`` are dict rows keyed by those
    headers (as polars ``iter_rows(named=True)`` / csv ``DictReader`` produce).
    Returns a :class:`KpiFormatDetection` carrying per-column role + evidence +
    confidence, plus nesting signals and any missing required roles — everything
    the confirmation panel needs to show "here is my understanding" before use.
    """
    columns = list(columns)
    col_values: dict[str, list[str]] = {
        col: [str(r.get(col) or "").strip() for r in rows] for col in columns
    }

    # Two separate scores per (column, role):
    #  - assign_score drives WHICH role a column gets. Decisive content for a
    #    REQUIRED role (e.g. a column that is ~all aggregate expressions) overrides
    #    a competing header match, so a misleadingly-named column (header "Detail"
    #    but values like `sum(...)`) is still recognised as the metric.
    #  - confidence is what we DISPLAY. A header-confirmed role is HIGH; anything
    #    inferred from values (or a weak header) is capped just below HIGH so it is
    #    always surfaced for confirmation, never treated as silently certain.
    assign_score: dict[tuple[int, str], float] = {}
    confidence_of: dict[tuple[int, str], float] = {}
    for idx, col in enumerate(columns):
        for role in CANONICAL_ROLES:
            h = _header_score(col, role)
            c = _content_score(col_values[col], role)
            a = max(h, c * 0.8)
            if role in REQUIRED_ROLES and c >= 0.9:
                a = max(a, 1.5)  # decisive required-role content wins assignment
            assign_score[(idx, role)] = a
            confidence_of[(idx, role)] = h if h >= _HIGH else min(max(h, c), _HIGH - 0.01)

    # Greedy global assignment: each role claimed by its single highest-scoring
    # column, each column assigned at most one role. Resolves the "two columns
    # both look like cuts" conflict deterministically by score.
    assigned_role_of_col: dict[int, str] = {}
    claimed_roles: set[str] = set()
    ranked = sorted(assign_score.items(), key=lambda kv: kv[1], reverse=True)
    for (idx, role), score in ranked:
        if score < _MED:
            continue
        if idx in assigned_role_of_col or role in claimed_roles:
            continue
        assigned_role_of_col[idx] = role
        claimed_roles.add(role)

    def _reason(col: str, role: str | None) -> str:
        if role is None:
            return "no KPI role matched header or column values above threshold"
        if _header_score(col, role) >= _HIGH:
            return f"header matches the `{role}` role"
        return f"column values signal `{role}` (header was absent/ambiguous)"

    column_roles: list[ColumnRole] = []
    unmapped: list[str] = []
    for idx, col in enumerate(columns):
        role = assigned_role_of_col.get(idx)
        confidence = confidence_of[(idx, role)] if role else 0.0
        if role is None:
            unmapped.append(col)
        column_roles.append(
            ColumnRole(
                header=col,
                column_index=idx,
                role=role,
                confidence=confidence,
                confidence_label=_label(confidence) if role else "LOW",
                evidence=_evidence(col_values[col]),
                reason=_reason(col, role),
            )
        )

    missing_required = [r for r in REQUIRED_ROLES if r not in claimed_roles]
    mapped_confidences = [c.confidence for c in column_roles if c.role]
    overall = min(mapped_confidences) if mapped_confidences else 0.0
    if missing_required:
        overall = min(overall, _MED - 0.01)  # cannot be HIGH if a required role is missing

    name_col = next((c for c in column_roles if c.role == "business_question"), None)
    name_header = name_col.header if name_col else None
    name_col_index = name_col.column_index if name_col else None
    nesting_suspected, nesting_signals = _detect_nesting(
        rows, name_header, name_col_index=name_col_index, merged_spans=merged_spans,
    )

    return KpiFormatDetection(
        source=source,
        columns=column_roles,
        row_count=len(rows),
        overall_confidence=overall,
        overall_label=_label(overall),
        unmapped_headers=unmapped,
        missing_required_roles=missing_required,
        nesting_suspected=nesting_suspected,
        nesting_signals=nesting_signals,
    )


__all__ = [
    "CANONICAL_ROLES",
    "REQUIRED_ROLES",
    "ROLE_HEADER_SYNONYMS",
    "ColumnRole",
    "KpiFormatDetection",
    "detect_kpi_format",
]

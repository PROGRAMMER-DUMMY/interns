from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


SQL_KEYWORDS = {
    "and",
    "as",
    "asc",
    "between",
    "by",
    "case",
    "cast",
    "desc",
    "distinct",
    "disitnct",
    "else",
    "end",
    "false",
    "from",
    "group",
    "having",
    "in",
    "is",
    "like",
    "not",
    "null",
    "or",
    "order",
    "over",
    "partition",
    "select",
    "then",
    "true",
    "when",
    "where",
}
# Generic English/business stopwords. Workspace-specific filter values
# (e.g. "Medicare", "Commercial") used to live here; they now come in via
# the `workspace_filter_terms` parameter to `extract_expression`, derived
# from `interns/generated/contracts/workspace_vocabulary.json` per
# workspace. Zero domain vocabulary hardcoded.
BUSINESS_TEXT_STOPWORDS = {
    "a",
    "above",
    "across",
    "actual",
    "all",
    "an",
    "at",
    "average",
    "banded",
    "benchmark",
    "but",
    "confirm",
    "dev",
    "dimension",
    "dimensions",
    "divided",
    "expected",
    "expired",
    "falls",
    "filing",
    "flag",
    "for",
    "grain",
    "high",
    "highest",
    "if",
    "it",
    "last",
    "low",
    "mean",
    "medium",
    "metric",
    "minus",
    "multiplied",
    "next",
    "no",
    "number",
    "of",
    "on",
    "outside",
    "past",
    "per",
    "percentage",
    "plus",
    "score",
    "share",
    "std",
    "that",
    "the",
    "this",
    "times",
    "top",
    "total",
    "touching",
    "track",
    "trend",
    "unplanned",
    "unique",
    "using",
    "weight",
    "weighted",
    "with",
    "within",
}
COMMON_FUNCTIONS = {
    "avg",
    "coalesce",
    "count",
    "date_diff",
    "datediff",
    "floor",
    "greatest",
    "lower",
    "max",
    "min",
    "month",
    "nullif",
    "percentile",
    "round",
    "sum",
    "upper",
}
# A bare "P95"-style percentile-literal reference in formula prose ("ChargeAmount
# > P95 within ICD group") is not a column -- it names a statistical rank. It
# survives strip_literals() (a letter+digit token, not a pure-digit one) and the
# identifier regex (a valid Python-identifier shape), so it needs its own filter.
# ponytail: P1-P999 range covers standard percentile notation (P0, P25, P50, P75, P99);
# expand to P\d+ if decimal percentiles (P99.5) appear in formula text.
_PERCENTILE_LITERAL_RE = re.compile(r"^p\d{1,3}$", re.IGNORECASE)


@dataclass(frozen=True)
class ExtractedExpression:
    identifiers: list[str]
    functions: list[dict[str, Any]] = field(default_factory=list)


def extract_expression(
    expression: str,
    *,
    workspace_filter_terms: list[str] | set[str] | None = None,
    known_columns: list[str] | set[str] | None = None,
) -> ExtractedExpression:
    """Extract identifiers from a metric/cut expression.

    `workspace_filter_terms` is the workspace-derived filter vocabulary
    (e.g., `LineOfBusiness` values like "Medicare", "Commercial" for one
    workspace; "Retail", "Wholesale" for another). Callers pass it from
    `core.onboarding.lexicon.vocabulary.terms_for(layout, "filter_terms")`.
    None or empty means no workspace research has been done yet — the
    extractor still works, it just won't filter out filter-value tokens.

    `known_columns` is the workspace's REAL physical column names (callers
    pass the resolver's `available_columns`, built from the schema index).
    A token matching one of them case-insensitively survives extraction
    even when it is also formula/stopword vocabulary. The stopword lists
    are workspace-agnostic by design, which is exactly why they over-reach:
    "High"/"Low" are banding words in one workspace and the canonical OHLC
    columns in the next; "Score"/"Weight"/"Flag" are ordinary column names
    somewhere. Only the workspace's own schema can tell a generic word
    apart from a legitimate business column, so schema evidence wins.
    None or empty keeps the pre-existing behaviour exactly.
    """
    cleaned = strip_literals(expression)
    function_names = _function_names(cleaned)
    extra_stopwords: set[str] = set()
    if workspace_filter_terms:
        extra_stopwords = {str(term).lower() for term in workspace_filter_terms if term}
    real_columns = {str(col).lower() for col in known_columns or () if col}
    identifiers = []
    seen = set()
    for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", cleaned):
        token_norm = token.lower()
        # Checked before every skip: a real column always survives. That
        # includes the `token in function_names` check, because KPI prose
        # puts units in parens right after the column name ("Weight (kg)
        # per department"), which reads as a call to `Weight` to the regex.
        if token_norm in real_columns:
            if token_norm not in seen:
                identifiers.append(token)
                seen.add(token_norm)
            continue
        if (
            len(token) <= 1
            or token_norm in SQL_KEYWORDS
            or token_norm in COMMON_FUNCTIONS
            or token_norm in BUSINESS_TEXT_STOPWORDS
            or token_norm in extra_stopwords
            or token in function_names
            or _PERCENTILE_LITERAL_RE.match(token_norm)
        ):
            continue
        if token_norm not in seen:
            identifiers.append(token)
            seen.add(token_norm)
    return ExtractedExpression(
        identifiers=identifiers,
        functions=_function_contexts(cleaned),
    )


def strip_literals(expression: str) -> str:
    expression = re.sub(r"'(?:''|[^'])*'", " ", expression)
    expression = re.sub(r'"(?:""|[^"])*"', " ", expression)
    expression = re.sub(r"=\s*[A-Za-z_][A-Za-z0-9_]*", "= ", expression)
    return re.sub(r"\b\d+(?:\.\d+)?\b", " ", expression)


def _function_names(expression: str) -> set[str]:
    return set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression))


def _function_contexts(expression: str) -> list[dict[str, Any]]:
    contexts = []
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)", expression):
        name = match.group(1)
        args = [
            token
            for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", match.group(2))
            if token.lower() not in SQL_KEYWORDS
        ]
        contexts.append({"function": name, "arguments": args})
    return contexts

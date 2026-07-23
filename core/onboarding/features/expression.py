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
    "an",
    "average",
    "divided",
    "for",
    "highest",
    "it",
    "minus",
    "multiplied",
    "no",
    "number",
    "of",
    "percentage",
    "plus",
    "share",
    "that",
    "this",
    "times",
    "top",
    "total",
    "trend",
    "unique",
    "with",
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


@dataclass(frozen=True)
class ExtractedExpression:
    identifiers: list[str]
    functions: list[dict[str, Any]] = field(default_factory=list)


def extract_expression(
    expression: str,
    *,
    workspace_filter_terms: list[str] | set[str] | None = None,
) -> ExtractedExpression:
    """Extract identifiers from a metric/cut expression.

    `workspace_filter_terms` is the workspace-derived filter vocabulary
    (e.g., `LineOfBusiness` values like "Medicare", "Commercial" for one
    workspace; "Retail", "Wholesale" for another). Callers pass it from
    `core.onboarding.lexicon.vocabulary.terms_for(layout, "filter_terms")`.
    None or empty means no workspace research has been done yet — the
    extractor still works, it just won't filter out filter-value tokens.
    """
    cleaned = strip_literals(expression)
    function_names = _function_names(cleaned)
    extra_stopwords: set[str] = set()
    if workspace_filter_terms:
        extra_stopwords = {str(term).lower() for term in workspace_filter_terms if term}
    identifiers = []
    seen = set()
    for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", cleaned):
        token_norm = token.lower()
        if (
            len(token) <= 1
            or token_norm in SQL_KEYWORDS
            or token_norm in COMMON_FUNCTIONS
            or token_norm in BUSINESS_TEXT_STOPWORDS
            or token_norm in extra_stopwords
            or token in function_names
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

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
BUSINESS_TEXT_STOPWORDS = {
    "across",
    "above",
    "average",
    "base",
    "claim",
    "commercial",
    "for",
    "highest",
    "medicaid",
    "medicare",
    "number",
    "of",
    "percentage",
    "share",
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


def extract_expression(expression: str) -> ExtractedExpression:
    cleaned = strip_literals(expression)
    function_names = _function_names(cleaned)
    identifiers = []
    seen = set()
    for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", cleaned):
        token_norm = token.lower()
        if (
            token_norm in SQL_KEYWORDS
            or token_norm in COMMON_FUNCTIONS
            or token_norm in BUSINESS_TEXT_STOPWORDS
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

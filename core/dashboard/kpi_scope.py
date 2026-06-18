"""Recover a KPI's intrinsic scope from its OWN generated SQL.

A KPI's scope -- the WHERE it was computed under (e.g. ``LineOfBusiness =
'Commercial'``, ``age > 50``) -- lives only in its generated SQL, not in any
structured contract. To re-source a KPI chart onto the row-grain table while
keeping it meaning the same, we need that scope as structured, overridable
filters. This module parses it generically from the platform's own deterministic
SQL (the ``kpi_*_results`` view), with NO domain words or per-workspace rules.

It is deliberately conservative: anything it can't fully and unambiguously map to
a conformed column -- a top-level ``OR``, an unrecognised predicate, an
expression with no matching SELECT alias -- makes the whole parse bail
(``None``). The caller then leaves the KPI gold-bound. A partial/guessed scope
would silently change what the KPI means, so we never emit one.

Returned scope shape (keys are conformed column names):
    {"LineOfBusiness": "Commercial", "age": {"gt": 50}}
Range ops use ``from``/``to`` (>=, <=) and ``gt``/``lt`` (>, <), matching the
filter forms the query engine understands.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_RESULTS_RE = re.compile(
    r'CREATE\s+OR\s+REPLACE\s+VIEW\s+"[^"]*_results"\s+AS\s+(SELECT\b.*?)(?:;|\Z)',
    re.IGNORECASE | re.DOTALL,
)
_OP_RE = re.compile(r"^(.*?)(>=|<=|=|>|<)(.*)$", re.DOTALL)
_IDENT_RE = re.compile(r'^"?([A-Za-z_]\w*)"?$')
_ALIAS_RE = re.compile(r'^(.*)\s+AS\s+"?([A-Za-z_]\w*)"?\s*$', re.IGNORECASE | re.DOTALL)
_RANGE_KEY = {">": "gt", "<": "lt", ">=": "from", "<=": "to"}


def _norm(expr: str) -> str:
    """Whitespace/quote-insensitive form, for matching a WHERE expression to a
    SELECT alias's defining expression."""
    return re.sub(r"\s+", " ", str(expr).replace('"', "")).strip().lower()


def _ci(name: str, cols: list[str]) -> Optional[str]:
    if name in cols:
        return name
    low = name.lower()
    return next((c for c in cols if c.lower() == low), None)


def _split_top(text: str, sep_words: tuple[str, ...]) -> Optional[list[str]]:
    """Split on top-level (paren depth 0) keyword separators. Returns the parts,
    or None if a forbidden separator (e.g. OR) appears at the top level."""
    parts: list[str] = []
    depth = 0
    cur = []
    i = 0
    n = len(text)
    upper = text.upper()
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0:
            for w in (" OR ",):
                if upper[i:i + len(w)] == w:
                    return None      # top-level OR -> not safely capturable
            matched = False
            for w in sep_words:
                if upper[i:i + len(w)] == w:
                    parts.append("".join(cur))
                    cur = []
                    i += len(w)
                    matched = True
                    break
            if matched:
                continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _split_commas_top(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _literal(rhs: str) -> Any:
    rhs = rhs.strip()
    m = re.match(r"^'(.*)'$", rhs, re.DOTALL)
    if m:
        return m.group(1).replace("''", "'")
    try:
        return int(rhs)
    except ValueError:
        pass
    try:
        return float(rhs)
    except ValueError:
        return None


def scope_from_sql(sql: str, fact_columns: list[str]) -> Optional[dict[str, Any]]:
    """Parse the KPI's intrinsic equality+range scope, mapped to conformed
    columns. ``{}`` when the KPI has no WHERE; ``None`` when any part can't be
    safely captured (caller must then NOT re-root)."""
    block_m = _RESULTS_RE.search(sql or "")
    if not block_m:
        return None
    block = block_m.group(1)

    sel_m = re.search(r"\bSELECT\b(.*?)\bFROM\b", block, re.IGNORECASE | re.DOTALL)
    aliases: dict[str, str] = {}
    if sel_m:
        for item in _split_commas_top(sel_m.group(1)):
            am = _ALIAS_RE.match(item.strip())
            if am:
                aliases[_norm(am.group(1))] = am.group(2)

    where_m = re.search(r"\bWHERE\b(.*?)(\bGROUP\s+BY\b|\bORDER\s+BY\b|\Z)",
                        block, re.IGNORECASE | re.DOTALL)
    if not where_m or not where_m.group(1).strip():
        return {}
    preds = _split_top(where_m.group(1), (" AND ",))
    if preds is None:
        return None

    scope: dict[str, Any] = {}
    for pred in preds:
        om = _OP_RE.match(pred.strip())
        if not om:
            return None
        lhs, op, rhs = om.group(1).strip(), om.group(2), om.group(3).strip()
        ln = _norm(lhs)
        col = aliases.get(ln)
        if col is None:
            idm = _IDENT_RE.match(lhs.strip())
            col = idm.group(1) if idm else None
        if col is None:
            return None
        fc = _ci(col, fact_columns)
        if fc is None:
            return None
        val = _literal(rhs)
        if val is None:
            return None
        if op == "=":
            scope[fc] = val
        else:
            d = scope.get(fc) if isinstance(scope.get(fc), dict) else {}
            d[_RANGE_KEY[op]] = val
            scope[fc] = d
    return scope


__all__ = ["scope_from_sql"]

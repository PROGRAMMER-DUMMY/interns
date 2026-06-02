"""Chart-type inference from KPI registry shape.

Workspace-agnostic. Inputs: the KPI's definition (business_question, metric,
cuts, filters) + the columns the generated SQL produces. Outputs: a chart
spec dict that the renderer can use directly. The picker is intentionally
small and rule-based — the user can override any field via `user_overrides`.

SQL column parsing
------------------
`parse_result_view_columns(sql_text, kpi_id)` extracts the SELECT-alias list
from the ``kpi_XXX_results`` CREATE VIEW statement.  The generator always
emits ``AS <alias>`` for every output column so a regex pass is sufficient.

Validation
----------
`validate_spec_columns(spec, result_columns)` returns a list of field-level
errors (empty = clean).  The caller (``build_kpi_spec`` / ``refresh_*``)
decides how to surface them — the function never raises so the dashboard still
renders a recovery card rather than crashing.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_DATE_NAME_PATTERN = re.compile(
    r"(date|month|year|week|day|time|timestamp|created|service|invoice|filed|posted)",
    re.IGNORECASE,
)
_TOP_N_PATTERN = re.compile(r"\btop\s*(\d+)\b", re.IGNORECASE)

# Matches  "...  AS alias"  where alias is an unquoted identifier.
# Also handles double-quoted aliases: AS "alias".
_SELECT_ALIAS_RE = re.compile(
    # A real select-item alias is followed by a comma (next item) or the end of
    # the SELECT clause. The trailing lookahead excludes CAST type annotations
    # such as `CAST("DOB" AS DATE)` / `CAST(x AS DOUBLE)`, where `AS <TYPE>` is
    # followed by `)` and must NOT be mistaken for an output column.
    r"""\bAS\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))(?=\s*(?:,|$))""",
    re.IGNORECASE,
)

# Aggregate-function / numeric-column name patterns that indicate a measure.
_MEASURE_NAME_RE = re.compile(
    r"(sum|count|avg|mean|total|amount|paid|revenue|share|percent|rate|ratio|"
    r"value|metric|kpi|score|qty|quantity|volume)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# SQL column extraction
# ---------------------------------------------------------------------------

def parse_result_view_columns(sql_text: str, kpi_id: str) -> list[str]:
    """Return the ordered list of column aliases from the ``kpi_XXX_results`` view.

    Parses the ``CREATE … VIEW "kpi_XXX_results" AS SELECT …`` block.  Works on
    the standard single-statement-per-view layout emitted by the KPI generator.

    Returns an empty list when the view block cannot be located (e.g. the SQL
    has not yet been generated).  Never raises.
    """
    if not sql_text or not kpi_id:
        return []
    # Locate the results-view block: from "kpi_XXX_results" AS SELECT to the
    # next CREATE statement or end of string.
    view_pattern = re.compile(
        rf"""CREATE\s+OR\s+REPLACE\s+VIEW\s+["']?{re.escape(kpi_id)}_results["']?\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|$)""",
        re.IGNORECASE | re.DOTALL,
    )
    m = view_pattern.search(sql_text)
    if not m:
        return []
    view_body = m.group(1)
    # Extract the SELECT clause: everything between SELECT and FROM (first FROM).
    select_m = re.search(r"\bSELECT\b(.*?)\bFROM\b", view_body, re.IGNORECASE | re.DOTALL)
    if not select_m:
        # Might be a SELECT DISTINCT …
        select_m = re.search(
            r"\bSELECT\s+DISTINCT\b(.*?)\bFROM\b", view_body, re.IGNORECASE | re.DOTALL
        )
    if not select_m:
        # Fallback: grab all AS aliases anywhere in the view body.
        return _extract_all_aliases(view_body)
    select_clause = select_m.group(1)
    return _extract_all_aliases(select_clause)


def _extract_all_aliases(text: str) -> list[str]:
    """Return all AS-alias values found in *text*, preserving order."""
    aliases: list[str] = []
    seen: set[str] = set()
    for m in _SELECT_ALIAS_RE.finditer(text):
        alias = m.group(1) or m.group(2)
        if alias and alias.lower() not in seen:
            aliases.append(alias)
            seen.add(alias.lower())
    return aliases


# ---------------------------------------------------------------------------
# Measure inference
# ---------------------------------------------------------------------------

def infer_measure_column(result_columns: list[str]) -> str:
    """Pick the best measure alias from *result_columns*.

    Preference order:
    1. Last column whose name matches an aggregate/numeric pattern.
    2. Last column overall (generator puts the measure last by convention).

    Returns an empty string when *result_columns* is empty.
    """
    if not result_columns:
        return ""
    # Score each column: higher = more measure-like.
    best = ""
    for col in result_columns:
        if _MEASURE_NAME_RE.search(col):
            best = col
    return best or result_columns[-1]


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------

# Fields that must be present in result_columns when non-empty.
_AXIS_FIELDS = ("x", "y", "color")


class SpecColumnError(Exception):
    """Raised by `validate_spec_columns` when strict=True and violations exist."""


def validate_spec_columns(
    spec: dict[str, Any],
    result_columns: list[str],
    *,
    strict: bool = False,
) -> list[str]:
    """Validate that axis fields in *spec* exist in *result_columns*.

    Parameters
    ----------
    spec:
        The ``machine_defaults`` dict (or merged config).
    result_columns:
        Column aliases from the result view (as returned by
        ``parse_result_view_columns``).
    strict:
        When True, raise ``SpecColumnError`` instead of returning a list.

    Returns
    -------
    list[str]
        Human-readable error strings.  Empty list means the spec is valid.
        When *result_columns* is empty the check is skipped (columns not yet
        known — SQL not yet generated).
    """
    if not result_columns:
        return []
    col_set = set(result_columns)
    errors: list[str] = []
    for field in _AXIS_FIELDS:
        value = str(spec.get(field) or "")
        if not value:
            continue  # unset fields are not validated
        if value not in col_set:
            errors.append(
                f"spec field '{field}' = '{value}' does not exist in result view columns "
                f"{result_columns}"
            )
    if strict and errors:
        raise SpecColumnError("; ".join(errors))
    return errors


# ---------------------------------------------------------------------------
# Cut parsing
# ---------------------------------------------------------------------------

def _split_cuts(cuts_text: str) -> list[str]:
    """Generic cut parser. Splits on commas and 'and'; trims parenthesized notes."""
    if not cuts_text:
        return []
    parts = re.split(r"[,;]| and ", cuts_text, flags=re.IGNORECASE)
    out: list[str] = []
    for part in parts:
        cleaned = re.sub(r"\(.*?\)", "", part).strip()
        if cleaned:
            out.append(cleaned)
    return out


def _is_date_like(name: str) -> bool:
    return bool(_DATE_NAME_PATTERN.search(name or ""))


def _detect_top_n(definition: dict[str, Any]) -> int | None:
    haystack = " ".join(
        [
            str(definition.get("business_question") or ""),
            str(definition.get("name") or ""),
            str(definition.get("description") or ""),
        ]
    )
    match = _TOP_N_PATTERN.search(haystack)
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Main inference entry point
# ---------------------------------------------------------------------------

def infer_chart(
    *,
    definition: dict[str, Any],
    result_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Return a default chart spec for a KPI.

    Rules (in order):

    1. Any cut name matches date/time pattern → ``line`` over time, color by next cut.
    2. Business question contains "top N" → ``ranked_bar`` with ``limit=N``.
    3. Single dimensional cut → ``bar``.
    4. Two dimensional cuts → ``grouped_bar``.
    5. No cuts → ``big_number`` (single-metric tile).

    ``x`` and ``color`` are resolved to the closest real result-view column alias
    when *result_columns* is provided.  ``y`` is resolved to the real measure alias
    (the last aggregate/numeric column) rather than the literal ``"value"``
    placeholder.  When *result_columns* is empty the inferred raw cut label is used
    as-is and validation is deferred until columns are available.

    Returns a plain dict — never raises. Callers store this under
    ``machine_defaults`` and let users override per field via ``user_overrides``.
    """
    metric = str(definition.get("metric") or "").strip()
    cuts_text = str(definition.get("cuts") or "").strip()
    cuts = _split_cuts(cuts_text)
    business_question = str(definition.get("business_question") or definition.get("name") or "").strip()

    available = list(result_columns or [])
    available_lower = {c.lower(): c for c in available}

    def _resolve_dim_column(name: str) -> str:
        """Resolve a cut label to the best matching result-view alias.

        Matching priority:
        1. Exact match.
        2. Case-insensitive exact match.
        3. Alphanumeric-stripped case-insensitive match (e.g. "Department Name" -> "departmentname").
        4. Prefix match: result column starts with the cleaned cut label.
        5. Substring match: result column contains the cleaned cut label (len >= 3).
        6. Return raw name unchanged (columns not yet known or no match).
        """
        if not name:
            return ""
        if name in available:
            return name
        lower = available_lower.get(name.lower())
        if lower:
            return lower
        cleaned = re.sub(r"[^A-Za-z0-9]+", "", name).lower()
        for candidate in available:
            if re.sub(r"[^A-Za-z0-9]+", "", candidate).lower() == cleaned:
                return candidate
        # Prefix / substring fallback
        if len(cleaned) >= 3:
            for candidate in available:
                c_clean = re.sub(r"[^A-Za-z0-9]+", "", candidate).lower()
                if c_clean.startswith(cleaned) or cleaned in c_clean:
                    return candidate
        return name  # unresolved — will surface as a validation error

    def _resolve_measure() -> str:
        """Return the real measure alias from result_columns, or '' when unknown."""
        return infer_measure_column(available) if available else ""

    date_cut = next((cut for cut in cuts if _is_date_like(cut)), "")
    non_date_cuts = [cut for cut in cuts if cut != date_cut]
    top_n = _detect_top_n(definition)

    measure = _resolve_measure()

    spec: dict[str, Any] = {
        "title": business_question or metric or "KPI",
        "metric": metric,
        "y_label": metric,
    }

    if date_cut:
        y_val = measure or "value"
        spec.update(
            {
                "chart_type": "line",
                "x": _resolve_dim_column(date_cut),
                "x_label": date_cut,
                "y": y_val,
                "agg": "sum",
                "color": _resolve_dim_column(non_date_cuts[0]) if non_date_cuts else "",
            }
        )
        return spec

    if top_n and cuts:
        primary = cuts[0]
        y_val = measure or "value"
        spec.update(
            {
                "chart_type": "ranked_bar",
                "x": _resolve_dim_column(primary),
                "x_label": primary,
                "y": y_val,
                "agg": "sum",
                "limit": top_n,
                "sort": "desc",
            }
        )
        return spec

    if len(cuts) == 1:
        y_val = measure or "value"
        spec.update(
            {
                "chart_type": "bar",
                "x": _resolve_dim_column(cuts[0]),
                "x_label": cuts[0],
                "y": y_val,
                "agg": "sum",
            }
        )
        return spec

    if len(cuts) >= 2:
        y_val = measure or "value"
        spec.update(
            {
                "chart_type": "grouped_bar",
                "x": _resolve_dim_column(cuts[0]),
                "x_label": cuts[0],
                "y": y_val,
                "agg": "sum",
                "color": _resolve_dim_column(cuts[1]),
            }
        )
        return spec

    spec.update(
        {
            "chart_type": "big_number",
            "y": measure or "value",
            "agg": "sum",
        }
    )
    return spec


__all__ = [
    "infer_chart",
    "infer_measure_column",
    "parse_result_view_columns",
    "validate_spec_columns",
    "SpecColumnError",
]

"""Chart-type inference from KPI registry shape.

Workspace-agnostic. Inputs: the KPI's definition (business_question, metric,
cuts, filters) + the columns the generated SQL produces. Outputs: a chart
spec dict that the renderer can use directly. The picker is intentionally
small and rule-based -- the user can override any field via `user_overrides`.

SQL column parsing
------------------
`parse_result_view_columns(sql_text, kpi_id)` extracts the SELECT-alias list
from the ``kpi_XXX_results`` CREATE VIEW statement. The generator always emits
``AS <alias>`` for every output column so a regex pass is sufficient.

Validation
----------
`validate_spec_columns(spec, result_columns)` returns a list of field-level
errors (empty = clean). The caller decides how to surface them -- the function
never raises so the dashboard still renders a recovery card rather than
crashing.

All examples in comments are generic (orders/customers/segments); no workspace
domain vocabulary is baked into logic -- behavior is derived from the metric
text, the cut labels, and the emitted result-view column names.
"""
from __future__ import annotations

import re
from typing import Any

from core.dashboard.model.cuts import headline_agg


_DATE_NAME_PATTERN = re.compile(
    r"(date|month|year|week|day|time|timestamp|created|service|invoice|filed|posted)",
    re.IGNORECASE,
)
_TOP_N_PATTERN = re.compile(r"\btop\s*(\d+)\b", re.IGNORECASE)

# A share / percentage / ratio metric. Matched against the metric text and
# against measure-column names (e.g. an emitted ``percentage_share`` alias).
# Generic -- keyed on the math vocabulary (percent/share/ratio/proportion),
# never on any workspace's domain words.
_SHARE_NAME_PATTERN = re.compile(
    r"(percentage|percent|share|ratio|proportion|\bpct\b|%)",
    re.IGNORECASE,
)

# Matches  "...  AS alias"  where alias is an unquoted identifier, or a
# double-quoted alias: AS "alias". A real select-item alias is followed by a
# comma (next item) or the end of the SELECT clause. The trailing lookahead
# excludes CAST type annotations such as ``CAST("x" AS DATE)`` where ``AS
# <TYPE>`` is followed by ``)`` and must NOT be mistaken for an output column.
_SELECT_ALIAS_RE = re.compile(
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

    Parses the ``CREATE OR REPLACE VIEW "kpi_XXX_results" AS SELECT ...`` block.
    Returns an empty list when the view block cannot be located (e.g. the SQL
    has not yet been generated). Never raises.
    """
    if not sql_text or not kpi_id:
        return []
    view_pattern = re.compile(
        rf"""CREATE\s+OR\s+REPLACE\s+VIEW\s+["']?{re.escape(kpi_id)}_results["']?\s+AS\s+(.*?)(?=CREATE\s+OR\s+REPLACE\s+VIEW|$)""",
        re.IGNORECASE | re.DOTALL,
    )
    m = view_pattern.search(sql_text)
    if not m:
        return []
    view_body = m.group(1)
    select_m = re.search(r"\bSELECT\b(.*?)\bFROM\b", view_body, re.IGNORECASE | re.DOTALL)
    if not select_m:
        select_m = re.search(
            r"\bSELECT\s+DISTINCT\b(.*?)\bFROM\b", view_body, re.IGNORECASE | re.DOTALL
        )
    if not select_m:
        # Fallback: grab all AS aliases anywhere in the view body.
        return _extract_all_aliases(view_body)
    return _extract_all_aliases(select_m.group(1))


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
    2. Last column overall (the generator puts the measure last by convention).

    Returns an empty string when *result_columns* is empty.
    """
    if not result_columns:
        return ""
    best = ""
    for col in result_columns:
        if _MEASURE_NAME_RE.search(col):
            best = col
    return best or result_columns[-1]


def _find_share_measure(result_columns: list[str]) -> str:
    """Return the first result column whose name reads as a percentage/share
    measure (e.g. ``percentage_share``), or '' when none qualifies."""
    for col in result_columns:
        if _SHARE_NAME_PATTERN.search(col):
            return col
    return ""


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

    Returns a list of human-readable error strings. Empty list means the spec
    is valid. When *result_columns* is empty the check is skipped (columns not
    yet known -- SQL not yet generated). With ``strict=True`` raises
    ``SpecColumnError`` instead of returning a list.
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


def _is_share_metric(metric: str, share_measure: str) -> bool:
    """True when the KPI is a share/percentage/ratio metric.

    Two independent signals (either is sufficient):
    1. The metric text mentions percent/share/ratio/proportion.
    2. The result view emitted a percentage/share-named measure column.
    """
    if share_measure:
        return True
    return bool(_SHARE_NAME_PATTERN.search(metric or ""))


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

def _infer_chart_branches(
    *,
    definition: dict[str, Any],
    result_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Return a default chart spec for a KPI.

    Rules (in precedence order):

    1. A date/time cut is present (trend) -> ``line`` over time; x = the date
       column, y = the measure, color by the next cut.
    2. The business question says "top N" -> ``ranked_bar`` (rendered as a
       horizontal bar sorted by the measure descending, limited to N).
    3. A share / percentage / ratio metric cut by a categorical dimension ->
       ``stacked_bar_percent`` (100%-stacked bar). Stacked-percent is chosen
       over grouped so each x-category sums to 100% and parts-of-a-whole read
       directly off the axis -- the literal intent of a share metric. The
       measure is the emitted percentage/share column when one exists, and the
       y-axis is formatted as a percentage (``y_format = "percent"``).
    4. A single categorical cut -> vertical ``bar``.
    5. Two or more categorical cuts -> ``grouped_bar``.
    6. No usable dimension -> ``big_number`` (single-value card).

    ``x`` and ``color`` are resolved to the closest real result-view column
    alias when *result_columns* is provided. ``y`` is resolved to the real
    measure alias (the share column for share metrics, else the last
    aggregate/numeric column) rather than the literal ``"value"`` placeholder.
    When *result_columns* is empty the inferred raw cut label is used as-is and
    validation is deferred until columns are available.

    Returns a plain dict -- never raises. Callers store this under
    ``machine_defaults`` and let users override per field via ``user_overrides``.
    """
    metric = str(definition.get("metric") or "").strip()
    cuts_text = str(definition.get("cuts") or "").strip()
    cuts = _split_cuts(cuts_text)
    business_question = str(
        definition.get("business_question") or definition.get("name") or ""
    ).strip()

    available = list(result_columns or [])
    available_lower = {c.lower(): c for c in available}

    def _resolve_dim_column(name: str) -> str:
        """Resolve a cut label to the best matching result-view alias.

        Priority: exact -> case-insensitive exact -> alnum-stripped match ->
        prefix/substring match -> raw name (unresolved, surfaces in validation).
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
        if len(cleaned) >= 3:
            for candidate in available:
                c_clean = re.sub(r"[^A-Za-z0-9]+", "", candidate).lower()
                if c_clean.startswith(cleaned) or cleaned in c_clean:
                    return candidate
        return name

    share_measure = _find_share_measure(available)
    measure = (share_measure or infer_measure_column(available)) if available else ""

    date_cut = next((cut for cut in cuts if _is_date_like(cut)), "")
    non_date_cuts = [cut for cut in cuts if cut != date_cut]
    top_n = _detect_top_n(definition)
    is_share = _is_share_metric(metric, share_measure)

    spec: dict[str, Any] = {
        "title": business_question or metric or "KPI",
        "metric": metric,
        "y_label": metric,
    }

    # 1. Trend over time -> line.
    if date_cut:
        spec.update(
            {
                "chart_type": "line",
                "x": _resolve_dim_column(date_cut),
                "x_label": date_cut,
                "y": measure or "value",
                "agg": "sum",
                "color": _resolve_dim_column(non_date_cuts[0]) if non_date_cuts else "",
            }
        )
        if is_share:
            spec["y_format"] = "percent"
            spec["y_label"] = metric or "share"
        return spec

    # 2. Top-N ranking -> horizontal ranked bar.
    if top_n and cuts:
        primary = cuts[0]
        spec.update(
            {
                "chart_type": "ranked_bar",
                "x": _resolve_dim_column(primary),
                "x_label": primary,
                "y": measure or "value",
                "agg": "sum",
                "limit": top_n,
                "sort": "desc",
                "orientation": "h",
            }
        )
        return spec

    # 3. Share / percentage metric over a categorical cut -> 100%-stacked bar.
    if is_share and cuts:
        primary = cuts[0]
        secondary = cuts[1] if len(cuts) >= 2 else ""
        spec.update(
            {
                "chart_type": "stacked_bar_percent",
                "x": _resolve_dim_column(primary),
                "x_label": primary,
                "y": measure or "value",
                "agg": "sum",
                "color": _resolve_dim_column(secondary) if secondary else "",
                "y_format": "percent",
                "y_label": metric or "share",
            }
        )
        return spec

    # 4. Single categorical cut -> vertical bar.
    if len(cuts) == 1:
        spec.update(
            {
                "chart_type": "bar",
                "x": _resolve_dim_column(cuts[0]),
                "x_label": cuts[0],
                "y": measure or "value",
                "agg": "sum",
            }
        )
        return spec

    # 5. Two or more categorical cuts -> grouped bar.
    if len(cuts) >= 2:
        spec.update(
            {
                "chart_type": "grouped_bar",
                "x": _resolve_dim_column(cuts[0]),
                "x_label": cuts[0],
                "y": measure or "value",
                "agg": "sum",
                "color": _resolve_dim_column(cuts[1]),
            }
        )
        return spec

    # 6. No usable dimension -> single-value card.
    spec.update(
        {
            "chart_type": "big_number",
            "y": measure or "value",
            "agg": "sum",
        }
    )
    if is_share:
        spec["y_format"] = "percent"
    return spec


__all__ = [
    "infer_chart",
    "infer_measure_column",
    "parse_result_view_columns",
    "validate_spec_columns",
    "SpecColumnError",
]


def infer_chart(
    *,
    definition: dict[str, Any],
    result_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Default chart spec for a KPI, with measure semantics normalised.

    `_infer_chart_branches` picks chart type and axes across six branches, each
    of which historically hardcoded ``"agg": "sum"``. Normalising here rather
    than in every branch means a NEW branch cannot reintroduce the defect this
    guards: a share rendered with ``y_format: percent`` and ``agg: sum``, i.e.
    summed percentages (2026-07-26 audit).

    `cuts.headline_agg` has always encoded the rule -- the branches simply never
    asked it.
    """
    spec = _infer_chart_branches(definition=definition, result_columns=result_columns)
    metric = str((definition or {}).get("metric") or "")
    measure = str(spec.get("y") or "")
    y_format = str(spec.get("y_format") or "")
    if y_format.lower() == "percent" or _SHARE_NAME_PATTERN.search(metric)             or _SHARE_NAME_PATTERN.search(measure):
        spec["agg"] = headline_agg(metric, measure, y_format or "percent")
    return spec
